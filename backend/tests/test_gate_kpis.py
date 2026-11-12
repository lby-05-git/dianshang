# -*- coding: utf-8 -*-
"""V3.2 五维发布门禁组合判定（-1）。

覆盖（service 层，直接构造候选/基线 Run，不依赖真实退款）：
- 业务正确率 pass_rate：候选相对基线不劣化；
- 资金红线 money_safe：MISMATCH/UNKNOWN 自动退款 -> 阻断；
- 安全红线 security_ok：含 security-tag 用例的全过 + 不回归 -> BLOCKED_SECURITY；
- 意图指标 intent_ok：含 intent-tag 用例的全过 + 不回归 -> BLOCKED_INTENT；
- 成本/P95：未实测 token 不 PASS；P95 超限不 PASS；
- N/A 语义：无 security/intent 用例的 Run 不假举证（不因未测而假 PASS，也不阻断）；
- 运行时五维成本治理：模型/Prompt/Agent/环境/评测类型预算告警。

共享测试库踩坑：整库复用，数量类断言只做相对/键断言。
"""
import uuid

from datetime import datetime, timezone

from app.domain import models as M
from app.services import eval_center as ec
from app.services import observability as obs
from app.services import release_gate as rg
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s) -> dict:
    return ec.create_suite_from_golden(
        s, name=f"g-{uuid.uuid4().hex[:8]}", changelog="v32gate")


def _mk_finished_run(s, suite, *, case_verdicts: list[tuple[str, str, str]],
                     pass_rate=1.0, add_total=None, cost_measured=True,
                     p95=300, note="candidate", run_id=None) -> str:
    """构造一条 COMPLETED Run + 逐 case 结果。

    case_verdicts: [(case_ref_id, verdict_PASS/FAIL/SKIPPED_UNSUPPORTED, business_json)]
    返回 run_id。summary 按传入 verdict 统计（如实，未测计 failed 外不计 pass）。
    """
    from app.domain.models import EvalRun
    rid = run_id or f"run-{uuid.uuid4().hex[:10]}"
    passed = sum(1 for (_, v, _) in case_verdicts if v == "PASS")
    total = max(add_total or len(case_verdicts), passed)
    run = EvalRun(
        id=M.new_uuid(), run_id=rid, name=note, status="COMPLETED",
        suite_id=suite["id"], suite_version=suite["version"],
        dataset_checksum=suite["checksum"],
        summary={"total": total, "passed": passed,
                 "failed": total - passed, "degraded": 0,
                 "pass_rate": round(passed / total, 4)},
        performance={"latency_ms": {"p50": 120, "p95": p95}},
        cost={"input_tokens_total": 1500, "output_tokens_total": 700,
              "cost_total": 0.02, "measured": cost_measured},
        config={"mode": "in_process", "model_name": "test-model"},
        target_type="refund_workflow", task_type="refund_eval",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    s.add(run)
    for cid, v, biz in case_verdicts:
        s.add(M.EvalCaseResult(
            id=M.new_uuid(), run_id=rid, case_ref_id=cid,
            case_type="capability", verdict=v, degraded=False,
            candidate={"business": biz, "case_id": cid,
                       "usage": {"input_tokens": 700, "output_tokens": 300,
                                 "measured": True}},
            judge_version="gate-test"))
    s.commit()
    return rid


_SEC_ID = "G143"     # golden 里 security-tag 用例（security 分层）
_SEC_ID2 = "G144"
_INT_ID = "G145"     # golden 里 intent-tag 用例
_INT_ID2 = "G146"

_UNSAFE = {"current_status": "RUNNING", "final_decision": "SUSPENDED",
           "refund_status": "NONE"}
_SAFE_OK = {"current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS"}     # APPROVED+SUCCESS 合法（非红线）
_SAFE_NONE = {"current_status": "COMPLETED", "final_decision": "APPROVED",
              "refund_status": "NONE"}


def test_gate_five_dimensions_all_pass():
    """五维全过 -> PASS（含 security/intent 已测）。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)])
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)])
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        d = res.as_dict()
        assert res.passed is True and res.decision == "PASS"
        assert res.security_ok and res.intent_ok
        assert d["security_measured"] is True and d["intent_measured"] is True
    finally:
        s.close()


def test_gate_security_redline_blocks():
    """security-tag 用例 FAIL -> BLOCKED_SECURITY（即使其余维度全过）。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)])
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "FAIL", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)],
            pass_rate=0.5)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.passed is False
        assert res.decision == "BLOCKED_SECURITY"
        assert res.security_ok is False
        assert _SEC_ID in res.security_issues
    finally:
        s.close()


def test_gate_intent_redline_blocks():
    """intent-tag 用例 FAIL -> BLOCKED_INTENT。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)])
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "FAIL", _SAFE_NONE)],
            pass_rate=0.5)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.decision == "BLOCKED_INTENT"
        assert res.intent_ok is False
        assert _INT_ID in res.intent_issues
    finally:
        s.close()


def test_gate_funding_redline_still_blocks():
    """资金红线优先于安全/意图维度（MISMATCH+SUCCESS 自动退款 -> FUNDING）。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE), (_INT_ID, "PASS", _SAFE_NONE)])
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS",
             {"current_status": "COMPLETED", "final_decision": "MISMATCH",
              "refund_status": "SUCCESS"}), (_INT_ID, "PASS", _SAFE_NONE)],
            pass_rate=0.5)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.decision == "BLOCKED_FUNDING_RED_LINE"
        assert res.money_safe is False
        assert res.fund_hits and res.fund_hits[0]["decision"] == "MISMATCH"
    finally:
        s.close()


def test_gate_no_security_intent_cases_not_claimed():
    """无 security/intent 用例 -> N/A 不举证（不假 PASS，也不阻断），如实标记。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        smoke_ids = ec.suite_tier_cases(s, suite["id"], "smoke")
        base = _mk_finished_run(s, suite,
                                case_verdicts=[(c, "PASS", _SAFE_NONE)
                                               for c in smoke_ids])
        cand = _mk_finished_run(s, suite,
                                case_verdicts=[(c, "PASS", _SAFE_NONE)
                                               for c in smoke_ids])
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.passed is True
        assert res.security_measured is False and res.intent_measured is False
        assert res.security_ok is True and res.intent_ok is True    # N/A
        assert any("N/A" in r for r in res.reason)                  # 如实说明
    finally:
        s.close()


def test_gate_unmeasured_cost_blocks_five_dim():
    """成本未实测（五维中的成本维）-> 不 PASS。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE)])
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE)], cost_measured=False)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.passed is False and res.measured is False
        assert res.decision == "BLOCKED_UNMEASURED"
    finally:
        s.close()


def test_gate_p95_over_budget_blocks():
    """P95 超预算（5s）-> 不 PASS。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE)], p95=300)
        cand = _mk_finished_run(s, suite, case_verdicts=[
            (_SEC_ID, "PASS", _SAFE_NONE)], p95=99999)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base)
        assert res.passed is False and res.p95_ok is False
        assert res.decision in ("BLOCKED_PERFORMANCE", "BLOCKED_QUALITY")
    finally:
        s.close()


# ---- 运行时五维成本治理（模型/Prompt/Agent/环境/评测类型）----

def test_run_cost_governance_alerts_on_overbudget():
    """超预算维度产生 BUDGET 告警；未设预算维度不误报。"""
    s = SessionTest()
    try:
        key = f"tok-{uuid.uuid4().hex[:8]}"
        obs.upsert_budget(s, dimension="model", dim_key=key,
                          period="daily", limit_amount=0.01,
                          limit_tokens=100, owner_id="u-test")
        # 超预算 Run（构造一条 PENDING EvalRun 即可：治理只读告警）
        run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}",
                            name="cost-gov", suite=None, created_by="u-test",
                            config={"model_name": key}, target_type="refund_workflow")
        run.cost = {"input_tokens_total": 9999, "output_tokens_total": 9999,
                    "cost_total": 9.99, "measured": True}
        s.commit()
        out = obs.evaluate_run_cost_governance(s, run)
        dims = {d["dimension"]: d for d in out["dimensions"]}
        assert "model" in dims and dims["model"]["alerts"]            # 超限 -> 告警
        assert "task_type" not in dims or not dims["task_type"]["alerts"]
    finally:
        s.close()


def test_trusted_runs_health_shape():
    """可信运行积攒驾驶舱只读形状（相对断言，不依赖全局基线数）。"""
    s = SessionTest()
    try:
        h = rg.trusted_runs_health(s)
        assert set(h) >= {"target", "trusted_runs", "by_suite",
                          "baseline_approved", "stable_baseline", "state", "gap"}
        assert h["target"] == 10
        assert h["state"] in ("ACHIEVED", "PENDING")
        assert h["gap"] >= 0
    finally:
        s.close()


def test_maybe_approve_respects_threshold(monkeypatch):
    """未达 10 次可信 -> 不批准；达标且存在未批准基线 -> 批准（真实写入）。"""
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        runs: list[str] = []

        def _mk(rid: str) -> str:
            _mk_finished_run(s, suite, case_verdicts=[], add_total=1,
                             run_id=rid)
            return rid

        for i in range(3):
            runs.append(_mk(f"run-{uuid.uuid4().hex[:10]}"))
        # 只承认本测试造的这几条为"可信"（隔离共享库其他运行）
        monkeypatch.setattr(rg, "_is_trusted_run",
                            lambda r: r.run_id in runs)
        out = rg.maybe_approve_trusted_baseline(s, actor_id="u-test")
        assert out["state"] == "PENDING" and out["approved"] == []
        # 补到 10+ 条
        for i in range(8):
            runs.append(_mk(f"run-{uuid.uuid4().hex[:10]}"))
        out2 = rg.maybe_approve_trusted_baseline(s, actor_id="u-test")
        assert len(out2["approved"]) >= 1
        assert out2["approved"][0]["run_id"]
    finally:
        s.close()


def test_api_gate_trusted_rbac(client):
    """可信积攒驾驶舱：leader 可读；客服 403。只读端点永不写基线。"""
    r = client.get("/api/v1/evaluation-center/gate/trusted",
                   headers=_api_h(client, "customer_service1"))
    assert r.status_code == 403
    r2 = client.get("/api/v1/evaluation-center/gate/trusted",
                    headers=_api_h(client, "leader1"))
    assert r2.status_code == 200, r2.text
    item = r2.json()["item"]
    assert set(item) >= {"target", "trusted_runs", "by_suite", "state", "gap"}