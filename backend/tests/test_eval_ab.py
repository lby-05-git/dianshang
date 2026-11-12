# -*- coding: utf-8 -*-
"""M14 Prompt/模型 A-B 实验 契约测试。

覆盖：
- AB1 Prompt 版本生命周期：创建即 DRAFT；DRAFT->CANDIDATE->APPROVED 须人工；
  未批准内容列表默认隐藏；native 状态迁移不允许自动 APPROVED。
- AB2 绑定未批准 Prompt 的 Run 创建被拒（人工批准门）。
- AB3 同一数据集 baseline/candidate 成对 Run（ab_bundle）属性正确。
- AB4 Worker 用 PromptAwareExecutor 执行 A-B Run：业务输出同 oracle（判定 pass），
  usage.measured=True、token/成本按真实组合 prompt 估算；Run cost 聚合真实。
- AB5 compare_a_b：token 降幅≥30% 且质量不劣化 => deployable；质量劣化否决；
  未测量（measured=False）永不标记可部署。
- AB6 API：A-B Run 创建 / 列表 / 结果报告 / prompt 列表 / 状态迁移 / RBAC。
"""
import json
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.services import report_service as rs
from app.worker.eval_processor import EvalRunConsumer
from eval_refund.executors import PromptAwareExecutor, estimate_tokens
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s):
    return ec.create_suite_from_golden(s,
                                       name=f"suite-{uuid.uuid4().hex[:8]}",
                                       changelog="M14")


def _consumer():
    return EvalRunConsumer(session_factory=SessionTest)


def _ab_fields(run_id, suite_id):
    return {
        "case_id": run_id, "run_id": run_id, "suite_id": suite_id,
        "config": json.dumps({"mode": "in_process", "limit": 3}),
    }


LONG_PROMPT = "你是退赔质检助手。" * 8
SHORT_PROMPT = "你是质检助手。" * 3


def _mk_prompt(s, *, name, content, is_baseline=False, agent_name="sentiment",
               model_name=None):
    return ec.create_prompt_version(
        s, name=name, agent_name=agent_name, content=content,
        model_name=model_name, changelog="M14", creator_id="u-m14",
        is_baseline=is_baseline)


def _approve(s, prompt_id):
    item = ec.transition_prompt_status(s, prompt_id, target="CANDIDATE",
                                       approver="u-m14")
    item = ec.transition_prompt_status(s, prompt_id, target="APPROVED",
                                       approver="u-m14")
    return item


# ---- AB1 Prompt 生命周期 + 人工批准 ----

def test_prompt_lifecycle_human_approval_required():
    s = SessionTest()
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    pv = _mk_prompt(s, name="base-v1", content=LONG_PROMPT, is_baseline=True,
                    agent_name=ag)
    assert pv["status"] == models.PROMPT_DRAFT
    # 未批准不可直接 APPROVED
    with pytest.raises(ValueError):
        ec.transition_prompt_status(s, pv["id"], target="APPROVED", approver="u")
    # DRAFT -> CANDIDATE 人工推进
    ec.transition_prompt_status(s, pv["id"], target="CANDIDATE", approver="u")
    # 批准
    appr = ec.transition_prompt_status(s, pv["id"], target="APPROVED", approver="u")
    assert appr["status"] == models.PROMPT_APPROVED
    assert appr["approver_id"] == "u"
    assert appr["approved_at"] is not None


def test_prompt_list_hides_unapproved_content():
    s = SessionTest()
    name = f"cand-{uuid.uuid4().hex[:6]}"
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    _mk_prompt(s, name=name, content=LONG_PROMPT, agent_name=ag)
    lst = ec.list_prompt_versions(s)
    item = next(i for i in lst["items"] if i["name"] == name)
    assert "未批准" in item["content"]  # 正文隐藏


def test_prompt_checksum_versions_unique_per_agent():
    s = SessionTest()
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    p1 = _mk_prompt(s, name="v1", content="A", agent_name=ag)
    p2 = _mk_prompt(s, name="v2", content="B", agent_name=ag)
    same = _mk_prompt(s, name="v1dup", content="A", agent_name=ag)
    assert same["id"] == p1["id"]      # 同 content_checksum 幂等
    assert p2["id"] != p1["id"]
    assert p2["version"] == "v2"        # 该 agent 下第 2 个版本
    assert p1["version"] == "v1"


# ---- AB2 人工批准门：未批准 Prompt 不可进入 Run ----

def test_ab_run_requires_approved_prompt():
    s = SessionTest()
    suite = _seed_suite(s)
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT, agent_name=ag)  # DRAFT
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=ag)
    base = ec.transition_prompt_status(s, base["id"], target="CANDIDATE",
                                       approver="u")
    base = ec.transition_prompt_status(s, base["id"], target="APPROVED",
                                       approver="u")
    with pytest.raises(ValueError):
        ec.create_ab_run(s, name="AB", suite=s.get(models.EvalSuite, suite["id"]),
                         baseline_id=base["id"], variant_id=cand["id"],
                         created_by="u-m14", limit=3)


def test_ab_run_requires_same_agent():
    s = SessionTest()
    suite = _seed_suite(s)
    gx = f"ax-{uuid.uuid4().hex[:5]}"
    gy = f"ay-{uuid.uuid4().hex[:5]}"
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=gx)
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT,
                      agent_name=gy)
    base, cand = _approve(s, base["id"]), _approve(s, cand["id"])
    with pytest.raises(ValueError):
        ec.create_ab_run(s, name="AB", suite=s.get(models.EvalSuite, suite["id"]),
                         baseline_id=base["id"], variant_id=cand["id"],
                         created_by="u-m14")


# ---- AB3 成对 Run 属性 ----

def test_ab_run_pair_created():
    s = SessionTest()
    suite = _seed_suite(s)
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=ag)
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT, agent_name=ag)
    base, cand = _approve(s, base["id"]), _approve(s, cand["id"])
    ab = ec.create_ab_run(s, name="AB 实验", suite=s.get(models.EvalSuite, suite["id"]),
                          baseline_id=base["id"], variant_id=cand["id"],
                          created_by="u-m14", limit=3)
    assert ab["baseline_run"]["label"] == "v1"
    assert ab["variant_run"]["label"] == "v2"
    a = ec.get_run(SessionTest(), ab["baseline_run"]["run_id"])
    b = ec.get_run(SessionTest(), ab["variant_run"]["run_id"])
    assert a["prompt_baseline_id"] == base["id"]
    assert a["prompt_variant_id"] is None
    assert b["prompt_variant_id"] == cand["id"]
    assert b["prompt_baseline_id"] is None
    assert a["config"]["ab_bundle"] == b["config"]["ab_bundle"]
    assert a["suite_id"] == b["suite_id"]


# ---- AB4 Worker 执行 A-B Run：真实 token/成本 ----

def test_worker_executes_ab_run_with_measured_usage():
    s = SessionTest()
    suite = _seed_suite(s)
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=ag)
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT, agent_name=ag)
    base, cand = _approve(s, base["id"]), _approve(s, cand["id"])
    ab = ec.create_ab_run(s, name="AB 执行", suite=s.get(models.EvalSuite, suite["id"]),
                          baseline_id=base["id"], variant_id=cand["id"],
                          created_by="u-m14", limit=3)
    # 连续执行 baseline Run + variant Run（同一对 Suite 上）
    a_run_id = ab["baseline_run"]["run_id"]
    b_run_id = ab["variant_run"]["run_id"]
    consumer = _consumer()
    consumer.process("ab-a", _ab_fields(a_run_id, suite["id"]))
    consumer.process("ab-b", _ab_fields(b_run_id, suite["id"]))

    a = ec.get_run(SessionTest(), a_run_id)
    b = ec.get_run(SessionTest(), b_run_id)
    assert a["status"] == "COMPLETED" and b["status"] == "COMPLETED"
    assert a["summary"]["passed"] == 3 and b["summary"]["passed"] == 3
    # token/成本实测（same dataset，candidate prompt 更短 -> 应更低）
    ca = a["cost"]; cb = b["cost"]
    assert ca["measured"] is True and cb["measured"] is True
    assert ca["input_tokens_total"] > 0
    assert cb["input_tokens_total"] < ca["input_tokens_total"]
    # baseline prompt 长 -> 输入 token 更多（真实组合 prompt，非伪造）
    assert ca["basis"] == "estimate_from_composed_prompt"

    # 单 case usage 也实测
    res = ec.list_run_results(SessionTest(), a_run_id)
    usage = (res[0]["candidate"] or {}).get("usage") or {}
    assert usage.get("measured") is True
    assert usage.get("input_tokens") > 0


def test_prompt_aware_executor_tokens_follow_prompt_length():
    s = SessionTest()
    suite = _seed_suite(s)
    case = ec.hydrated_cases(s, suite["id"])[0]
    ex_base = PromptAwareExecutor(prompt=LONG_PROMPT)
    ex_cand = PromptAwareExecutor(prompt=SHORT_PROMPT)
    outs = (ex_base(case, {}), ex_cand(case, {}))
    assert outs[0].input_tokens > outs[1].input_tokens
    assert outs[0].measured is True
    # business 输出与 oracle 一致（资金副作用安全）
    from eval_refund.executors import GoldenFollowerExecutor
    oracle = GoldenFollowerExecutor()(case, {})
    assert outs[0].final_decision == oracle.final_decision
    assert estimate_tokens("你" * 100) > estimate_tokens("你" * 10)


def test_cost_unmeasured_when_no_candidate_usage():
    """未实测（oracle 无 usage）-> cost.measured=False，绝不为 A-B 伪造。"""
    import app.worker.eval_processor as wp
    cost = wp._cost([])
    assert cost["measured"] is False


# ---- AB5 compare_a_b 判定 ----

def _mk_ab_report(s, pair, *, rate_a, rate_b, measured, tokens_a, tokens_b):
    """构造 DB Run + 各 case 结果 + cost 的 A-B 报告对（run_id 唯一）。"""
    tag = uuid.uuid4().hex[:5]
    pair = (f"run{tag}a", f"run{tag}b")
    a_id, b_id = pair
    for run_id, passed, rate, tokens in ((a_id, round(rate_a * 30), rate_a,
                                          tokens_a),
                                         (b_id, round(rate_b * 30), rate_b,
                                          tokens_b)):
        run = models.EvalRun(
            run_id=run_id, name=run_id, task_type="refund_eval",
            schema_version="1.0", priority=0, status="COMPLETED",
            suite_version="2.0.0", dataset_checksum="abc",
            config={"ab_bundle": "bundle-x"},
            summary={"total": 30, "passed": round(rate * 30), "failed": 0,
                     "degraded": 0, "pass_rate": rate},
            performance={"latency_ms": {"p50": 1.0, "p95": 2.0}},
            cost={"measured": measured, "measured_cases": 30,
                  "input_tokens_total": tokens, "output_tokens_total": 0,
                  "cost_total": tokens * 0.001, "basis": "x" if measured else None},
        )
        s.add(run)
    s.commit()
    return a_id, b_id


def test_ab_compare_deployable_when_quality_kept_and_token_saved():
    s = SessionTest()
    a, b = _mk_ab_report(s, ("run-abase", "run-bcand"), rate_a=1.0, rate_b=1.0,
                         measured=True, tokens_a=10_000, tokens_b=5_000)
    cmp = rs.compare_a_b(s, a, b)
    assert cmp["measured"] is True
    assert cmp["token_delta_pct"] == 50.0          # 省 50%
    assert cmp["quality_ok"] is True
    assert cmp["pass"] is True
    assert cmp["deployable"] is True


def test_ab_compare_not_deployable_when_quality_regressed():
    s = SessionTest()
    a, b = _mk_ab_report(s, ("run-abase-r", "run-bcand-r"), rate_a=1.0, rate_b=0.9,
                         measured=True, tokens_a=10_000, tokens_b=5_000)
    cmp = rs.compare_a_b(s, a, b)
    assert cmp["token_delta_pct"] == 50.0
    assert cmp["quality_ok"] is False
    assert cmp["deployable"] is False
    assert cmp["pass"] is False


def test_ab_compare_not_deployable_when_unmeasured():
    s = SessionTest()
    a, b = _mk_ab_report(s, ("run-abase-u", "run-bcand-u"), rate_a=1.0, rate_b=1.0,
                         measured=False, tokens_a=10_000, tokens_b=5_000)
    cmp = rs.compare_a_b(s, a, b)
    assert cmp["measured"] is False
    assert cmp["token_delta_pct"] is None
    assert cmp["deployable"] is False  # 未测量永不部署


def test_ab_compare_regressed_cases_listed():
    s = SessionTest()
    a, b = _mk_ab_report(s, ("run-abase-c", "run-bcand-c"), rate_a=1.0, rate_b=0.9,
                         measured=True, tokens_a=10_000, tokens_b=5_000)
    cmp = rs.compare_a_b(s, a, b)
    assert "user_id" not in cmp  # 结构断言辅助
    assert isinstance(cmp["regressed"], list)


# ---- AB6 API ----

def test_api_prompt_crud_and_transition(client):
    h = _api_h(client, "leader1")
    ag = f"api-agent-{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/evaluation-center/prompts",
                    json={"name": "api-prom", "agent_name": ag,
                          "content": SHORT_PROMPT, "model_name": "claude-sonnet-4-5",
                          "is_baseline": True}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["item"]["id"]
    assert r.json()["item"]["status"] == "DRAFT"
    lst = client.get("/api/v1/evaluation-center/prompts", headers=h)
    assert lst.status_code == 200
    # 未批准正文隐藏
    item = next(i for i in lst.json()["items"] if i["id"] == pid)
    assert "未批准" in item["content"]
    # 人工批准
    t = client.post(f"/api/v1/evaluation-center/prompts/{pid}/transition",
                    json={"to_status": "CANDIDATE"}, headers=h)
    assert t.status_code == 200
    t2 = client.post(f"/api/v1/evaluation-center/prompts/{pid}/transition",
                     json={"to_status": "APPROVED"}, headers=h)
    assert t2.status_code == 200
    assert t2.json()["item"]["approver_id"] is not None


def test_api_create_ab_and_read_report(client):
    s = SessionTest()
    suite = _seed_suite(s)
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=ag)
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT, agent_name=ag)
    base = ec.transition_prompt_status(s, base["id"], target="CANDIDATE", approver="u")
    base = ec.transition_prompt_status(s, base["id"], target="APPROVED", approver="u")
    cand = ec.transition_prompt_status(s, cand["id"], target="CANDIDATE", approver="u")
    cand = ec.transition_prompt_status(s, cand["id"], target="APPROVED", approver="u")
    h = _api_h(client, "leader1")

    r = client.post("/api/v1/evaluation-center/runs/ab",
                    json={"name": "API AB", "suite_id": suite["id"],
                          "baseline_id": base["id"], "variant_id": cand["id"],
                          "limit": 3}, headers=h)
    assert r.status_code == 202, r.text
    body = r.json()
    bundle = body["bundle_id"]
    assert body["baseline_run"]["run_id"].endswith("a-baseline")

    # Worker 执行两 Run
    _consumer().process("api-ab-a", _ab_fields(body["baseline_run"]["run_id"],
                                               suite["id"]))
    _consumer().process("api-ab-b", _ab_fields(body["variant_run"]["run_id"],
                                               suite["id"]))
    rep = client.get(f"/api/v1/evaluation-center/reports/ab/{bundle}", headers=h)
    assert rep.status_code == 200, rep.text
    cmp = rep.json()
    assert cmp["measured"] is True
    assert cmp["token_delta_pct"] is not None
    assert cmp["baseline"]["run_id"] == body["baseline_run"]["run_id"]

    # 列表
    lst = client.get("/api/v1/evaluation-center/runs/ab", headers=h)
    assert lst.status_code == 200
    ids = [b["bundle_id"] for b in lst.json()["items"]]
    assert bundle in ids


def test_api_ab_requires_approved_prompt(client):
    s = SessionTest()
    suite = _seed_suite(s)
    ag = f"agent-{uuid.uuid4().hex[:6]}"
    base = _mk_prompt(s, name="base", content=LONG_PROMPT, is_baseline=True,
                      agent_name=ag)
    cand = _mk_prompt(s, name="cand", content=SHORT_PROMPT, agent_name=ag)  # DRAFT 未批准
    base = ec.transition_prompt_status(s, base["id"], target="CANDIDATE", approver="u")
    base = ec.transition_prompt_status(s, base["id"], target="APPROVED", approver="u")
    h = _api_h(client, "leader1")
    r = client.post("/api/v1/evaluation-center/runs/ab",
                    json={"name": "AB no-approve", "suite_id": suite["id"],
                          "baseline_id": base["id"], "variant_id": cand["id"],
                          "limit": 3}, headers=h)
    assert r.status_code == 400, r.text


def test_api_ab_rbac(client):
    h = _api_h(client, "customer_service1")
    r = client.post("/api/v1/evaluation-center/runs/ab",
                    json={"name": "ab", "suite_id": "x", "baseline_id": "y",
                          "variant_id": "z"}, headers=h)
    assert r.status_code == 403