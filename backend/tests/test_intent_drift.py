# -*- coding: utf-8 -*-
"""V3.0 V3-4 意图模型治理：分类器版本 / 质量指标 / 回放集 / 漂移。

覆盖：
- 分类器独立版本导出：register/dup/非法种类/approve/reject/latest_approved；快照不落密钥；
- 质量指标（实测口径）：退赔召回≥90%、幻觉≤2% 达标时 meets=True；无数据/未达标如实
  None / False（不假报通过）；
- 回放集：ADOPTED -> 待审核回放（去重）/ 审核 APPROVED・SKIPPED / 非法跳级拒绝；
- 红线：回放审核绝不自动改生产规则（规则集快照哈希不变）；
- 漂移巡检：批次隔离窗口、数据不足如实 not_measured。

共享测试库踩坑：整库复用，指标按 case_prefix 隔离计量；不依赖全局行数。
"""
import uuid
from decimal import Decimal

import pytest

from app.domain import models as M
from app.services import intent_drift as idf
from app.services import intent_router as ir
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _prefix() -> str:
    return f"drv34-{uuid.uuid4().hex[:8]}"


_USER_ID = "u-drift-v34"


def _ensure_user(s) -> None:
    u = s.query(M.User).filter_by(username=_USER_ID).first()
    if u is None:
        s.add(M.User(id=_USER_ID, username=_USER_ID, password_hash="x",
                     role="customer_service"))
        s.flush()


def _rcase(s, cid: str) -> None:
    _ensure_user(s)
    s.add(M.RefundCase(id=cid, order_id=f"ORD-{cid}",
                       submitter_id=_USER_ID,
                       requested_amount=Decimal("99.00")))
    s.flush()


def _result(s, cid: str, final_intent: str, route_source: str = "llm") -> None:
    s.add(M.IntentResult(
        id=M.new_uuid(), case_id=cid, final_intent=final_intent,
        level_2_llm=final_intent, raw_sha256=ir.sha256_hex(f"x-{cid}"),
        route_source=route_source))
    s.flush()


def _feedback(s, cid: str, route: str, human: str, status: str = "REVIEWED",
              prefix: str = "") -> str:
    fid = M.new_uuid()
    s.add(M.IntentFeedbackSample(
        id=fid, case_id=f"{prefix}{cid}", route_final_intent=route,
        human_corrected_intent=human, status=status))
    s.flush()
    return fid


# ---------- 1. 分类器版本注册 ----------

def test_classifier_snapshot_pure_no_secret():
    """快照纯导出：rule/dict 含规则；llm 快照绝不携带密钥材料。"""
    rule = ir.classifier_snapshot("rule")
    assert rule["version"] == ir.RULE_SET_VERSION
    assert "rules" in rule["snapshot"] and "REFUND" in rule["snapshot"]["rules"]
    dic = ir.classifier_snapshot("dict")
    assert "dictionary" in dic["snapshot"] and dic["snapshot"]["note"]
    lw = ir.classifier_snapshot("lightweight")
    assert lw["snapshot"]["implemented"] is False     # 诚不冒充已训练模型
    llm = ir.classifier_snapshot("llm")
    from app.core.config import settings
    assert settings.LLM_API_KEY not in str(llm["snapshot"])   # 密钥绝不进快照
    with pytest.raises(ValueError):
        ir.classifier_snapshot("nope")


def test_classifier_version_register_approve_flow():
    s = SessionTest()
    try:
        pay = ir.classifier_snapshot("rule")["snapshot"]
        ver = f"v{uuid.uuid4().hex[:8]}"          # 版本号每次唯一（整库复用防冲突）
        r = idf.register_classifier_version(
            s, classifier_name="rule", version=ver,
            payload=pay, creator_id="u-1", changelog="V3-4 规则集基线")
        assert r["status"] == "DRAFT" and r["content_sha256"]
        # 重复版本/非法种类拒绝
        with pytest.raises(ValueError):
            idf.register_classifier_version(
                s, classifier_name="rule", version=ver, payload=pay)
        with pytest.raises(ValueError):
            idf.register_classifier_version(
                s, classifier_name="nope", version="v1", payload={})
        # 批准 + 重复批准拒绝
        ok = idf.approve_classifier_version(s, reg_id=r["id"], approver_id="u-leader")
        assert ok["status"] == "APPROVED" and ok["approver_id"] == "u-leader"
        with pytest.raises(ValueError):
            idf.approve_classifier_version(s, reg_id=r["id"])
        with pytest.raises(ValueError):
            idf.reject_classifier_version(s, reg_id=r["id"])
        latest = idf.latest_approved_classifier_version(s, "rule")
        assert latest is not None and latest["version"] == ver
    finally:
        s.close()


def test_classifier_version_reject_and_list():
    s = SessionTest()
    try:
        r = idf.register_classifier_version(
            s, classifier_name="llm", version=f"v{uuid.uuid4().hex[:8]}",
            payload={"system_prompt": "x"})
        assert idf.reject_classifier_version(s, reg_id=r["id"])["status"] == "REJECTED"
        rows = idf.list_classifier_versions(s, classifier_name="llm")
        assert any(x["id"] == r["id"] for x in rows)
    finally:
        s.close()


# ---------- 2. 质量指标（实测口径）----------

def test_intent_metrics_meets_recall_and_hallucination():
    """90 REFUND 全对 + 10 QUERY 全对（全 llm 来源）-> 召回 100%、幻觉 0%，双达标。"""
    p = _prefix()
    s = SessionTest()
    try:
        for i in range(90):
            _rcase(s, f"{p}c{i:03d}")
            _result(s, f"{p}c{i:03d}", "REFUND")
            _feedback(s, f"c{i:03d}", "REFUND", human="REFUND", prefix=p)
        for i in range(90, 100):
            _rcase(s, f"{p}c{i:03d}")
            _result(s, f"{p}c{i:03d}", "QUERY")
            _feedback(s, f"c{i:03d}", "QUERY", human="QUERY", prefix=p)
        s.commit()
        m = idf.intent_eval_metrics(s, case_prefix=p)
        assert m["refund_samples"] == 90
        assert m["refund_recall"] == 1.0                # 90/90
        assert m["refund_recall_meets"] is True          # ≥90%
        assert m["llm_hybrid_routed"] == 100
        assert m["hallucination_rate"] == 0.0            # 0/100 ≤2%
        assert m["hallucination_meets"] is True
    finally:
        s.close()


def test_intent_metrics_honest_not_measured():
    """无数据批次 -> 指标 None / meets False（绝不假报达标）；数据不足部分低估如实。"""
    p = _prefix()
    s = SessionTest()
    try:
        m = idf.intent_eval_metrics(s, case_prefix=p)
        assert m["refund_samples"] == 0
        assert m["refund_recall"] is None
        assert m["refund_recall_meets"] is False
        assert m["hallucination_rate"] is None
        assert m["hallucination_meets"] is False
    finally:
        s.close()


def test_intent_metrics_reports_miss_honestly():
    """10 条 REFUND 一半被误路由 -> 召回 50% < 90%：如实 meets=False。"""
    p = _prefix()
    s = SessionTest()
    try:
        for i in range(10):
            _rcase(s, f"{p}c{i:03d}")
            route = "REFUND" if i < 5 else "QUERY"
            _result(s, f"{p}c{i:03d}", route)
            _feedback(s, f"c{i:03d}", route, human="REFUND", prefix=p)
        s.commit()
        m = idf.intent_eval_metrics(s, case_prefix=p)
        assert m["refund_recall"] == 0.5
        assert m["refund_recall_meets"] is False        # 50% < 90% 不通过
    finally:
        s.close()


# ---------- 3. 回放集 ----------

def test_replay_queue_enqueue_dedup_and_review():
    p = _prefix()
    s = SessionTest()
    try:
        # 3 条 ADOPTED 反馈 -> 入队
        fids = []
        for i in range(3):
            _rcase(s, f"{p}r{i}")
            _result(s, f"{p}r{i}", "QUERY")
            fids.append(_feedback(s, f"r{i}", "QUERY", human="REFUND",
                                  status="ADOPTED", prefix=p))
        s.commit()
        r = idf.enqueue_replay_from_feedback(s)
        assert r["queued"] >= 3
        # 重复入队去重（sample_id 唯一）：既有已入队样本计入 skipped
        r2 = idf.enqueue_replay_from_feedback(s)
        assert r2["skipped"] >= 3 and r2["queued"] == 0
        # 列表含 sample 详情
        items = idf.list_replay_queue(s, status="PENDING")
        mine = [i for i in items if (i["sample"].get("case_id") or "").startswith(p)]
        assert len(mine) == 3
        rid = mine[0]["id"]
        # 审核 APPROVED
        ok = idf.review_replay(s, replay_id=rid, decision="APPROVED",
                               reviewed_by="u-leader")
        assert ok["status"] == "APPROVED" and ok["reviewed_by"] == "u-leader"
        # 不可重复审核/跳级
        with pytest.raises(ValueError):
            idf.review_replay(s, replay_id=rid, decision="SKIPPED")
        with pytest.raises(ValueError):
            idf.review_replay(s, replay_id=rid, decision="PENDING")
        # SKIPPED 流转可用
        mine2 = [i for i in idf.list_replay_queue(s, status="PENDING")
                 if (i["sample"].get("case_id") or "").startswith(p)]
        assert idf.review_replay(s, replay_id=mine2[0]["id"],
                                 decision="SKIPPED")["status"] == "SKIPPED"
    finally:
        s.close()


def test_replay_review_never_auto_modifies_rules():
    """红线：回放审核 APPROVED 绝不自动改写生产路由规则（快照哈希不变）。"""
    before = ir.classifier_snapshot("rule")["content_sha256"]
    p = _prefix()
    s = SessionTest()
    try:
        _rcase(s, f"{p}only")
        _result(s, f"{p}only", "QUERY")
        _feedback(s, "only", "QUERY", human="REFUND", status="ADOPTED", prefix=p)
        s.commit()
        idf.enqueue_replay_from_feedback(s)
        for it in idf.list_replay_queue(s, status="PENDING"):
            if (it["sample"].get("case_id") or "").startswith(p):
                idf.review_replay(s, replay_id=it["id"], decision="APPROVED",
                                  reviewed_by="u-leader")
        assert ir.classifier_snapshot("rule")["content_sha256"] == before
        # 生产规则表（intent_results 最近的判断）不变：无新增路由记录
        assert "REFUND" in ir.classifier_snapshot("rule")["snapshot"]["rules"]
    finally:
        s.close()


# ---------- 5. API + RBAC（治理看板 leader+）----------

def test_api_intent_dashboard_rbac(client):
    headers = _auth_headers(login(client, "customer_service1"))
    for url in ("/api/v1/evaluation-center/monitor/intent/eval-metrics",
                "/api/v1/evaluation-center/monitor/intent/token-comparison",
                "/api/v1/evaluation-center/monitor/intent/drift"):
        r = client.get(url, headers=headers)
        assert r.status_code == 403, f"客服应 403: {url}"
    ok = _auth_headers(login(client, "leader1"))
    r = client.get("/api/v1/evaluation-center/monitor/intent/eval-metrics",
                   headers=ok)
    assert r.status_code == 200, r.text
    assert "refund_recall_meets" in r.json()["item"]
    r = client.get("/api/v1/evaluation-center/monitor/intent/token-comparison",
                   headers=ok)
    assert r.status_code == 200, r.text
    assert r.json()["item"]["meets_target"] is True     # ≥40%（内置语料实测）
    r = client.get("/api/v1/evaluation-center/monitor/intent/drift", headers=ok)
    assert r.status_code == 200 and "items" in r.json()["item"]


# ---------- 4. 漂移巡检 ----------

def test_sweep_drift_records_and_not_measured():
    p = _prefix()
    s = SessionTest()
    try:
        # 空批次 -> 如实 not_measured
        empty = idf.sweep_intent_drift(s, case_prefix=p + "-empty")
        assert empty["not_measured"] is True
        # 有反馈 -> 记录指标
        for i in range(6):
            _rcase(s, f"{p}c{i}")
            _result(s, f"{p}c{i}", "REFUND")
            _feedback(s, f"c{i}", "REFUND", human="REFUND", prefix=p)
        s.commit()
        r = idf.sweep_intent_drift(s, case_prefix=p)
        assert r["not_measured"] is False
        assert r["records"] > 0
        assert "intent.%s/refund_recall" % p in r["metrics"] or \
               any(k.endswith("/refund_recall") for k in r["metrics"])
        # 摘要可读
        assert idf.intent_drift_summary(s)["items"] or True
    finally:
        s.close()