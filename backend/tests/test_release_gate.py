# -*- coding: utf-8 -*-
"""M19 发布门禁 + 候选回放契约测试（V3.0 持续评测治理）。

覆盖：
- S1 service：基线批准（仅 COMPLETED）与切换唯一性；留痕 approver/approved_at。
- S2 gate：资金红线阻断 —— 候选 MISMATCH/UNKNOWN 触发自动退款 -> BLOCKED_FUNDING_RED_LINE。
- S3 gate：质量不劣化 —— 候选相对基线有回归 -> 不 PASS；无回归全绿 -> PASS。
- S4 gate：未测量成本 -> 不 PASS（绝不把未测当达标）。
- S5 API：approve / evaluate / results / replay-set 读写契约。
- S6 RBAC：CS 无权访问门禁/回放集。

红线底盘：门禁只读，不原地改 Run/数据集；replay-set 不执行、不改数据。
"""
import json
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.services import release_gate as rg
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s) -> dict:
    return ec.create_suite_from_golden(
        s, name=f"g-{uuid.uuid4().hex[:8]}", changelog="M19")


def _run_smoke(s, name="M19") -> dict:
    """跑一个 smoke tier Run（in_process，golden-follower）并返回完成后的 run dict。"""
    suite = _seed_suite(s)
    run = ec.create_run(
        s, run_id=f"run-{uuid.uuid4().hex[:10]}", name=name,
        suite=s.get(models.EvalSuite, suite["id"]),
        created_by="u-test",
        config={"mode": "in_process", "tier": "smoke"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "m19s1", {"case_id": run.run_id, "run_id": run.run_id,
                  "suite_id": suite["id"],
                  "config": json.dumps({"mode": "in_process", "tier": "smoke"})})
    return suite, ec.get_run(s, run.run_id)


def _run_smoke_on(s, suite, name="M19") -> dict:
    """在指定 suite 上跑一个 smoke tier Run（in_process，golden-follower）。"""
    run = ec.create_run(
        s, run_id=f"run-{uuid.uuid4().hex[:10]}", name=name,
        suite=s.get(models.EvalSuite, suite["id"]),
        created_by="u-test",
        config={"mode": "in_process", "tier": "smoke"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "m19s1", {"case_id": run.run_id, "run_id": run.run_id,
                  "suite_id": suite["id"],
                  "config": json.dumps({"mode": "in_process", "tier": "smoke"})})
    return ec.get_run(s, run.run_id)


def _fake_baseline_run(s, suite, run_id=None) -> dict:
    """构造一个基线 Run（不真实执行，直接落库结果用于 gate 对比）。"""
    if run_id is None:
        run_id = f"run-{uuid.uuid4().hex[:10]}"
    from datetime import datetime, timezone
    from app.domain.models import EvalRun
    run = EvalRun(
        id=models.new_uuid(), run_id=run_id, name="baseline", status="COMPLETED",
        suite_id=suite["id"], suite_version=suite["version"],
        dataset_checksum=suite["checksum"],
        summary={"total": 6, "passed": 6, "failed": 0, "degraded": 0,
                 "pass_rate": 1.0},
        performance={"latency_ms": {"p50": 100, "p95": 300}},
        cost={"input_tokens_total": 1000, "output_tokens_total": 500,
              "cost_total": 0.01, "measured": True},
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    s.add(run)
    from app.domain.models import EvalCaseResult
    smoke_ids = ec.suite_tier_cases(s, suite["id"], "smoke")
    for cid in smoke_ids:
        s.add(EvalCaseResult(
            id=models.new_uuid(), run_id=run_id, case_ref_id=cid,
            case_type="auto_approve", verdict="PASS", degraded=False))
    s.commit()
    return run_id


# ---- S1 基线批准 ----

def test_approve_baseline_only_completed_and_unique():
    s = SessionTest()
    suite = _seed_suite(s)
    # 非 COMPLETED 不可批准
    pending = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}",
                            name="pending", suite=s.get(models.EvalSuite, suite["id"]),
                            created_by="u")
    with pytest.raises(ValueError, match="COMPLETED"):
        rg.approve_baseline(s, run_id=pending.run_id, suite_id=suite["id"],
                            actor_id="u-lead")
    # 同一套件跑两次 smoke Run，先后批准 -> 切换唯一（历史基线被覆盖）
    run1 = _run_smoke_on(s, suite)
    run2 = _run_smoke_on(s, suite)
    bl1 = rg.approve_baseline(s, run_id=run1["run_id"], suite_id=suite["id"],
                              actor_id="u-lead", note="first")
    assert bl1["run_id"] == run1["run_id"]
    assert bl1["approved_by"] == "u-lead"
    bl2 = rg.approve_baseline(s, run_id=run2["run_id"], suite_id=suite["id"],
                              actor_id="u-lead2", note="switch")
    got = rg.get_baseline(s, suite["id"])
    assert got["run_id"] == run2["run_id"]
    # 历史基线被覆盖，不重复
    assert bl2["previous_run_id"] == run1["run_id"]


# ---- S2 资金红线 ----

def test_gate_funding_red_line_blocks():
    s = SessionTest()
    suite = _seed_suite(s)
    base_run = _fake_baseline_run(s, suite)
    # 候选：故意制造一条 MISMATCH 且 SUCCESS 退款（红线触发）
    cand_run = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=cand_run)
    # 覆盖候选 G01 的 candidate business 使其命中红线
    from app.domain.models import EvalCaseResult
    rows = (s.query(EvalCaseResult).filter_by(run_id=cand_run).all())
    target = rows[0]
    target.passed = None
    target.candidate = {"business": {"current_status": "COMPLETED",
                                     "final_decision": "MISMATCH",
                                     "refund_status": "SUCCESS"},
                        "case_id": target.case_ref_id}
    s.commit()
    res = rg.evaluate_release_gate(s, run_id=cand_run, suite_id=suite["id"],
                                   baseline_run_id=base_run)
    assert res.passed is False
    assert res.decision == "BLOCKED_FUNDING_RED_LINE"
    assert res.money_safe is False
    assert res.fund_hits and res.fund_hits[0]["case_id"] == target.case_ref_id


# ---- S3 质量不劣化 ----

def test_gate_quality_regression_and_pass():
    s = SessionTest()
    suite = _seed_suite(s)
    base_run = _fake_baseline_run(s, suite)
    # 候选全绿：应 PASS
    ok_run = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=ok_run)
    from app.domain.models import EvalCaseResult
    # 候选全部 PASS -> gate 通过
    for row in s.query(EvalCaseResult).filter_by(run_id=ok_run).all():
        row.candidate = {"business": {"current_status": "COMPLETED",
                                      "final_decision": "APPROVED",
                                      "refund_status": "NONE"},
                         "usage": {"input_tokens": 900, "output_tokens": 400,
                                   "measured": True}}
    s.commit()
    res_ok = rg.evaluate_release_gate(s, run_id=ok_run, suite_id=suite["id"],
                                      baseline_run_id=base_run)
    assert res_ok.passed is True
    assert res_ok.decision == "PASS"

    # 候选有一条回归（PASS->FAIL）-> 不 PASS
    reg_run = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=reg_run)
    rows = s.query(EvalCaseResult).filter_by(run_id=reg_run).all()
    reg = rows[-1]
    reg.verdict = "FAIL"
    reg.candidate = {"business": {"current_status": "COMPLETED",
                                  "final_decision": "REJECTED",
                                  "refund_status": "NONE"},
                     "usage": {"input_tokens": 900, "output_tokens": 400,
                               "measured": True}}
    s.commit()
    res_reg = rg.evaluate_release_gate(s, run_id=reg_run, suite_id=suite["id"],
                                       baseline_run_id=base_run)
    assert res_reg.passed is False
    assert reg_run in res_reg.regressed or res_reg.reason


# ---- S4 未测量成本 ----

def test_gate_unmeasured_cost_blocks():
    s = SessionTest()
    suite = _seed_suite(s)
    base_run = _fake_baseline_run(s, suite)
    cand_run = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=cand_run)
    # 红线本质：候选报告顶层 cost.measured=False（未实测 token/成本）-> 不 PASS
    from app.domain.models import EvalRun
    curr = s.query(EvalRun).filter_by(run_id=cand_run).first()
    curr.cost = {"input_tokens_total": 0, "output_tokens_total": 0,
                 "cost_total": 0.0, "measured": False}
    s.commit()
    res = rg.evaluate_release_gate(s, run_id=cand_run, suite_id=suite["id"],
                                   baseline_run_id=base_run)
    assert res.passed is False
    assert res.measured is False
    assert res.decision == "BLOCKED_UNMEASURED"


# ---- S5 API ----

def test_api_release_gate_flow(client):
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"g-{uuid.uuid4().hex[:6]}", changelog="M19 api", owner_id="u")
    h = _api_h(client, "leader1")

    # 批准基线（需要一个 COMPLETED Run）-> 我们直接落一个假的但状态正确的
    base_run = _fake_baseline_run(s, suite, run_id=f"run-{uuid.uuid4().hex[:10]}")
    r = client.post("/api/v1/evaluation-center/gate/baseline",
                    json={"suite_id": suite["id"], "run_id": base_run,
                          "note": "M19 baseline"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["item"]["run_id"] == base_run

    # 查基线
    bg = client.get(f"/api/v1/evaluation-center/gate/baseline/{suite['id']}",
                    headers=h)
    assert bg.status_code == 200
    assert bg.json()["item"]["run_id"] == base_run

    # 门禁判定：候选全绿 -> PASS
    cand = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=cand)
    ev = client.post("/api/v1/evaluation-center/gate/evaluate",
                     json={"run_id": cand, "suite_id": suite["id"],
                           "tier": "smoke"}, headers=h)
    assert ev.status_code == 200, ev.text
    assert ev.json()["item"]["passed"] is True
    assert ev.json()["item"]["decision"] == "PASS"

    # 历史门禁记录
    hist = client.get("/api/v1/evaluation-center/gate/results", headers=h)
    assert hist.status_code == 200
    assert hist.json()["items"] and hist.json()["items"][0]["passed"] is True

    # 回放集：core tier
    rp = client.post("/api/v1/evaluation-center/gate/replay-set",
                     json={"suite_id": suite["id"], "run_id": cand,
                           "tier": "core"}, headers=h)
    assert rp.status_code == 200, rp.text
    item = rp.json()["item"]
    assert item["tier"] == "core"
    assert item["case_count"] >= 100 # V3.0 -1：核心 ≥100 条
    assert ec.suite_tier_cases(s, suite["id"], "core") == sorted(item["case_ids"])

    # 回放集 hydration 契约
    rpc = client.get(
        f"/api/v1/evaluation-center/gate/replay-set/{suite['id']}/core",
        headers=h)
    assert rpc.status_code == 200
    assert len(rpc.json["cases"]) >= 100 # V3.0 -1：核心 ≥100 条

    # 资金红线（API 层证据链也阻断）
    bad = f"run-{uuid.uuid4().hex[:10]}"
    _fake_baseline_run(s, suite, run_id=bad)
    from app.domain.models import EvalCaseResult
    rows = s.query(EvalCaseResult).filter_by(run_id=bad).all()
    rows[1].candidate = {"business": {"current_status": "COMPLETED",
                                      "final_decision": "UNKNOWN",
                                      "refund_status": "SUCCESS"},
                         "case_id": rows[1].case_ref_id}
    s.commit()
    ev2 = client.post("/api/v1/evaluation-center/gate/evaluate",
                      json={"run_id": bad, "suite_id": suite["id"],
                            "tier": "smoke"}, headers=h)
    assert ev2.status_code == 200
    assert ev2.json()["item"]["decision"] in (
        "BLOCKED_FUNDING_RED_LINE", "BLOCKED_UNMEASURED")


# ---- S6 RBAC ----

def test_api_rbac_cs_cannot_gate(client):
    h = _api_h(client, "customer_service1")
    r = client.get("/api/v1/evaluation-center/gate/results", headers=h)
    assert r.status_code == 403
    r2 = client.post("/api/v1/evaluation-center/gate/replay-set",
                     json={"suite_id": "x", "run_id": "y", "tier": "core"},
                     headers=h)
    assert r2.status_code == 403