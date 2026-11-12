# -*- coding: utf-8 -*-
"""Phase C 成本分账。

覆盖：
- C01 成本按 agent/model/case 维度分账：PromptAware 业务执行器 usage 带 agent=business、
  真实 model + 费率快照；_cost 聚合 by_agent/by_model/by_case。
- C02 来源分账 by_source：{business, judge}；LLM judge 未捕获 usage 时 judge=0/
  measured=false（如实不伪造）；捕获后 tokens 记入 judge 源。
- C07 Worker 完成 Run 后自动触发预算检查（obs.check_budget）：超限产生 BUDGET 告警
  （run/model 维度），未配置预算则不告警、Run 状态不受影响。
"""
import json
import uuid

from app.domain import models
from app.services import eval_center as ec
from app.services import observability as obs
from app.worker.eval_processor import EvalRunConsumer, _cost
from tests.conftest import SessionTest


def _seed_suite(s):
    return ec.create_suite_from_golden(s,
                                       name=f"suite-{uuid.uuid4().hex[:8]}",
                                       changelog="C")


def _consumer():
    return EvalRunConsumer(session_factory=SessionTest)


def _fields(run_id, suite_id, limit=0, concurrency=0):
    return {
        "case_id": run_id, "run_id": run_id, "suite_id": suite_id,
        "config": json.dumps({"mode": "in_process", "limit": limit,
                              "concurrency": concurrency}),
    }


def _prompt(s, *, name, content, is_baseline=False, agent_name="sentiment",
            model_name="claude-sonnet-4-5"):
    tag = uuid.uuid4().hex[:6]
    return ec.create_prompt_version(
        s, name=name, agent_name=agent_name, content=f"{content} {tag}",
        model_name=model_name, changelog="C", creator_id="u-c",
        is_baseline=is_baseline)


def _approve(s, pid):
    ec.transition_prompt_status(s, pid, target="CANDIDATE", approver="u")
    return ec.transition_prompt_status(s, pid, target="APPROVED", approver="u")


# ---- C01：维度分账 ----

def test_cost_decomposed_by_agent_model_case():
    """_cost 聚合 by_agent/by_model/by_case：PromptAware 业务执行器 usage 归属正确。"""
    s = SessionTest()
    suite = _seed_suite(s)
    p = _prompt(s, name="base", content="你是质检助手。", is_baseline=True)
    _approve(s, p["id"])
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:8]}", name="C01",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process", "limit": 2},
                        prompt_baseline_id=p["id"])
    _consumer().process("c01", _fields(run.run_id, suite["id"], limit=2))

    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    cs = got["cost"]
    # 分账结构必现
    assert "by_agent" in cs and "by_model" in cs and "by_source" in cs and "by_case" in cs
    # business agent：measured + 真实 token
    ba = cs["by_agent"]["business"]
    assert ba["measured"] is True
    assert ba["measured_cases"] == 2
    assert ba["input_tokens"] > 0 and ba["cost"] > 0
    # model 维度：model_name 即用例注册费率快照
    bm = cs["by_model"]["claude-sonnet-4-5"]
    assert bm["measured_cases"] == 2
    assert bm["rates"] and "in" in bm["rates"]
    # by_case 逐用例
    assert len(cs["by_case"]) == 2
    one = next(iter(cs["by_case"].values()))
    assert one["agent"] == "business" and one["cost"] > 0


def test_cost_unmeasured_no_by_agent_invention():
    """oracle 无 usage -> measured=False，绝不伪造分账（by_* 为空即如实空）。"""
    cost = _cost([{"case_id": "x", "candidate": None}])
    assert cost["measured"] is False
    assert cost["by_agent"] == {}
    assert cost["by_source"]["judge"]["measured"] is False


# ---- C02：来源分账（business/judge）----

def test_cost_judge_source_usage_captured():
    """LLM judge 捕获 usage -> judge 源 tokens 如实计入（不伪造费率/金额）。"""
    result = {
        "case_id": "g1",
        "candidate": {
            "usage": {"measured": True, "input_tokens": 100, "output_tokens": 50,
                      "cost": 0.001, "agent": "business", "model": "m",
                      "model_rates": {"in": 3, "out": 15}},
        },
        "judge": {"llm": {"usage": {"input_tokens": 200, "output_tokens": 40}}},
    }
    cs = _cost([result])
    assert cs["measured"] is True
    src = cs["by_source"]                       # business + judge 两源
    assert src["business"]["tokens"] == 150
    assert src["judge"]["tokens"] == 240
    assert src["judge"]["measured"] is True
    # judge 无费率 -> cost 不为 judge 造假金额
    ja = cs["by_agent"]["judge"]
    assert ja["tokens"] == 240
    assert ja["cost"] == 0.0


def test_cost_judge_source_unmeasured_when_no_usage():
    """LLM judge 未配置/未捕获 -> judge 源 measured=False，不起任何金额。"""
    result = {"case_id": "g2", "candidate": {"usage": {"measured": True}},
              "judge": {"llm": None}}
    cs = _cost([result])
    assert cs["by_source"]["judge"]["measured"] is False
    assert cs["by_source"]["judge"]["tokens"] == 0


# ---- C07：Worker 完成后预算检查 ----

def test_worker_budget_check_alert_on_overrun():
    """Run 内 model 成本超预算 -> 自动 BUDGET 告警（C07 接线）。"""
    s = SessionTest()
    suite = _seed_suite(s)
    p = _prompt(s, name="base", content="你是质检助手。", is_baseline=True)
    _approve(s, p["id"])
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:8]}", name="C07",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process", "limit": 2},
                        prompt_baseline_id=p["id"])
    # 预算设为极小（0.00001）-> 一经执行必然超限
    obs.upsert_budget(s, dimension="run", dim_key=run.run_id, period="per_run",
                      limit_amount=0.00001, limit_tokens=1)
    _consumer().process("c07", _fields(run.run_id, suite["id"], limit=2))

    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"          # Run 完成不受预算超限影响
    alerts = obs.list_alerts(s, unacked_only=True)
    budget_alerts = [a for a in alerts if "BUDGET" in a["message"]]
    assert budget_alerts, f"应有 BUDGET 告警（model 超限）：{alerts}"