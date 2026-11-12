# -*- coding: utf-8 -*-
"""订单/退款一致性对账（V3.0 / 计划 V3-1）。

覆盖：
- check_case 资金红线：APPROVED+已退 而核验未全 MATCH -> CRITICAL；退后态未批准 -> CRITICAL；
- check_case 金额 diff：申请超订单可退 -> HIGH；
- run_reconciliation 幂等重跑：同 run 同差异不重复落审计（guid 唯一）；
- MultiSourceOrderProvider：信任加权共识 EXIST/MISSING/UNKNOWN + 熔断降级；
- SourceBreaker：连续失败 -> OPEN -> 冷却后半开探针；
- API：客服 403、主管 200/202；run/overview/metrics/resolve 形状。

共享测试库踩坑：整库复用，数量类断言只做相对/键断言，不做跨测试精确值。
"""
import pytest

from app.domain import models as M
from app.infrastructure.order_provider import (
    EXIST,
    MISSING,
    SOURCE_UNKNOWN,
    MockOrderProvider,
    MultiSourceOrderProvider,
    OrderRecord,
    SourceBreaker,
)
from app.services import reconciliation as recon
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _seed_user(db, username="customer_service1") -> M.User:
    u = db.query(M.User).filter_by(username=username).first()
    assert u is not None, "seed 应有演示账号"
    return u


def _mk_case(db, *, order_id="ORD-1001", amount=99.0, final=None, refund=None,
             ov="MATCH", pv="MATCH", prc="MATCH"):
    u = _seed_user(db)
    c = M.RefundCase(
        id=M.new_uuid(), order_id=order_id, order_no=f"NO{order_id[-4:]}",
        channel="APP", requested_amount=amount,
        submitter_id=u.id, current_status=M.CASE_STATUS_COMPLETED,
        final_decision=final or M.DECISION_APPROVED,
        refund_status=refund or M.REFUND_STATUS_SUCCESS,
        order_verification=ov, product_verification=pv, price_verification=prc,
    )
    db.add(c)
    db.commit()
    return c


def test_check_case_funding_red_line_critical():
    """已批准+已退 而核验未全 MATCH -> CRITICAL（资金红线，绝不让路）。"""
    s = SessionTest()
    try:
        c = _mk_case(s, ov="MISMATCH", pv="MATCH", prc="MATCH")
        diffs = recon.check_case(s, case=c)
        hits = [d for d in diffs if d["field"] == "refund_without_match_verify"]
        assert hits and hits[0]["severity"] == "CRITICAL"
    finally:
        s.close()


def test_check_case_refund_without_approval_critical():
    """退后态但决策未 APPROVED -> CRITICAL（绕过审批红线）。"""
    s = SessionTest()
    try:
        c = _mk_case(s, final=M.DECISION_REJECTED, refund=M.REFUND_STATUS_PENDING)
        diffs = recon.check_case(s, case=c)
        hits = [d for d in diffs if d["field"] == "refund_without_approval"]
        assert hits and hits[0]["severity"] == "CRITICAL"
    finally:
        s.close()


def test_check_case_amount_exceeds_refundable_high():
    """申请金额超订单可退 -> HIGH（ORD-1001 可退 99）。"""
    s = SessionTest()
    try:
        c = _mk_case(s, order_id="ORD-1001", amount=500.0)
        diffs = recon.check_case(s, case=c)
        hits = [d for d in diffs if d["field"] == "request_exceeds_refundable"]
        assert hits and hits[0]["severity"] == "HIGH"
    finally:
        s.close()


def test_run_reconciliation_idempotent_guid():
    """同 run 同案同差异 guid 唯一：重跑不重复落审计。"""
    s = SessionTest()
    try:
        c = _mk_case(s, order_id="ORD-1002", amount=9999.0, ov="MISMATCH")
        out1 = recon.run_reconciliation(s, run_id="recon-it-1", case_id=c.id)
        assert out1["examined"] == 1
        assert out1["by_severity"].get("CRITICAL", 0) >= 1
        n1 = s.query(M.ReconciliationEvent).filter_by(run_id="recon-it-1").count()
        out2 = recon.run_reconciliation(s, run_id="recon-it-1", case_id=c.id)
        assert out2["created"] == 0                 # 幂等：无新增
        n2 = s.query(M.ReconciliationEvent).filter_by(run_id="recon-it-1").count()
        assert n1 == n2 and n1 >= 1
    finally:
        s.close()


def test_run_record_domain_event_and_resolve():
    """对账运行落 RECONCILIATION_RUN 审计；resolve 只置位不删审计行。"""
    s = SessionTest()
    try:
        c = _mk_case(s, order_id="ORD-1001", amount=88.0)
        recon.run_reconciliation(s, run_id="recon-ev-1", case_id=c.id,
                                 actor_id="u-supervisor")
        evt = (s.query(M.DomainEvent)
               .filter_by(action="RECONCILIATION_RUN").first())
        assert evt is not None and evt.payload["created"] >= 0
        first = s.query(M.ReconciliationEvent).first()
        if first is not None:
            out = recon.resolve_alert(s, first.id, actor_id="u-supervisor")
            assert out is not None and out["resolved"] is True
            assert s.get(M.ReconciliationEvent, first.id) is not None  # 审计行保留
    finally:
        s.close()


# ---- 多来源核验契约 ----

def test_multi_source_consensus_exist_and_missing():
    """双源一致 -> EXIST；双源一致缺失 -> MISSING。"""
    mp = MultiSourceOrderProvider()
    a = MockOrderProvider(timeout_for_username=None)
    b = MockOrderProvider(timeout_for_username=None)
    mp.register_source("mock-a", a, trust=1.0)
    mp.register_source("mock-b", b, trust=0.6)
    res = mp.multi_lookup("ORD-1001")
    assert res["verdict"] == EXIST and res["order"] is not None
    assert res["sources"][0]["verdict"] == EXIST and res["sources"][1]["verdict"] == EXIST
    assert res["consensus"] is True and res["degraded"] is False
    res2 = mp.multi_lookup("ORD-NOPE-1")
    assert res2["verdict"] == MISSING and res2["order"] is None


def test_multi_source_conflict_unknown():
    """来源间矛盾（一源存在、一源缺失）-> UNKNOWN（应安全挂起，绝不自动退）。"""
    mp = MultiSourceOrderProvider()
    a = MockOrderProvider(timeout_for_username=None)          # 返回 ORD-1001
    b = MockOrderProvider(timeout_for_username=None)          # 空额外表 + 无内置
    b.register_order(OrderRecord(
        order_id="ORD-1001x", order_no="NO9999", user_username="customer_service1",
        status="PAID", total_price_yuan=1.0, paid_amount_yuan=1.0,
        refunded_amount_yuan=0.0, refundable_amount_yuan=1.0))
    mp.register_source("mock-a", a, trust=1.0)   # ORD-1001 存在
    mp.register_source("mock-b", b, trust=1.0)   # ORD-1001 不存在（额外表是其它订单）
    res = mp.multi_lookup("ORD-1001x")
    # mock-a 查 ORD-1001x -> 不存在；mock-b 查 -> 存在 => 矛盾 -> UNKNOWN
    assert res["verdict"] == SOURCE_UNKNOWN and res["consensus"] is False


def test_multi_source_circuit_breaker_degrade():
    """一源超时 -> 连续失败熔断 -> 其余源降级；全不可用 -> UNKNOWN + trust_used=0。"""
    mp = MultiSourceOrderProvider()
    ok = MockOrderProvider(timeout_for_username=None)
    down = MockOrderProvider(timeout_for_username="x")
    # down 源对 ORD-TIMEOUT-9 恒超时（Mock 内置规则：order_id 前缀 ORD-TIMEOUT）
    mp.register_source("mock-ok", ok, trust=1.0)
    mp.register_source("mock-down", down, trust=0.6)
    res = mp.multi_lookup("ORD-TIMEOUT-9")
    # mock-ok 无此订单 -> MISSING；mock-down 超时 -> UNKNOWN(degraded)
    # 有 MISSING 且无 EXIST -> MISSING；degraded=True（一源未用）
    assert res["verdict"] == MISSING
    assert res["degraded"] is True
    assert res["sources"][1]["degraded"] is True
    # 双源都超时 -> UNKNOWN
    mp2 = MultiSourceOrderProvider()
    for n in ("s1", "s2"):
        mp2.register_source(n, MockOrderProvider(timeout_for_username="x"), trust=1.0)
    res2 = mp2.multi_lookup("ORD-TIMEOUT-9")
    assert res2["verdict"] == SOURCE_UNKNOWN and res2["trust_used"] == 0.0


def test_source_breaker_open_half_open(monkeypatch):
    """连续失败 -> OPEN 拒绝；冷却过半开探针放行；成功后闭合。"""
    import time
    br = SourceBreaker(threshold=2, cooldown_seconds=5.0)
    assert br.state() == "CLOSED"
    br.record(False)
    br.record(False)
    assert br.state() == "OPEN"
    assert br.allow() is False          # 熔断期内拒绝
    # 冷却已过（模拟时间流逝）：半开 -> 放行探针 -> 成功闭合
    br._opened_at = time.time() - 5.1
    assert br.state() == "HALF_OPEN"
    assert br.allow() is True
    br.record(True)
    assert br.state() == "CLOSED"


# ---- 多来源判定留痕 ----

def test_record_source_verdicts_idempotent():
    """每来源判定落 OrderSourceVerdict；同案同源重跑不重复。"""
    s = SessionTest()
    try:
        c = _mk_case(s, order_id="ORD-1001", amount=10.0)
        mp = MultiSourceOrderProvider()
        mp.register_source("mock-a", MockOrderProvider(timeout_for_username=None), 1.0)
        mp.register_source("mock-b", MockOrderProvider(timeout_for_username=None), 0.6)
        res = mp.multi_lookup(c.order_id)
        n1 = recon.record_source_verdicts(s, case=c, multi_result=res)
        assert n1 == 2
        n2 = recon.record_source_verdicts(s, case=c, multi_result=res)
        assert n2 == 0          # 幂等
        rows = recon.list_source_verdicts(s, c.id)
        assert len(rows) == 2
    finally:
        s.close()


# ---- API ----

def test_reconciliation_api_permissions_and_shapes(client):
    """客服 403；主管 run/overview/metrics 可用。"""
    tok_cs = _auth(client, "customer_service1")
    tok_sup = _auth(client, "supervisor1")

    r = client.post("/api/v1/reconciliation/run",
                    json={}, headers={"Authorization": f"Bearer {tok_cs}"})
    assert r.status_code == 403, r.text
    r = client.get("/api/v1/reconciliation/overview",
                   headers={"Authorization": f"Bearer {tok_cs}"})
    assert r.status_code == 403, r.text

    r = client.post("/api/v1/reconciliation/run",
                    json={"window_minutes": 1440},
                    headers={"Authorization": f"Bearer {tok_sup}"})
    assert r.status_code == 202, r.text
    body = r.json()["item"]
    assert set(body) >= {"run_id", "examined", "created",
                         "by_severity", "by_category"}

    r = client.get("/api/v1/reconciliation/overview",
                   headers={"Authorization": f"Bearer {tok_sup}"})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert set(item) >= {"window_minutes", "total", "by_severity",
                         "by_category", "unresolved_alerts", "recent"}

    r = client.get("/api/v1/reconciliation/metrics",
                   headers={"Authorization": f"Bearer {tok_sup}"})
    assert r.status_code == 200, r.text
    mm = r.json()["item"]
    assert set(mm) >= {"window_minutes", "verify_total", "verify_fail_rate",
                       "refund_fail_rate", "refund_retries", "inconsistency_rate"}

    r = client.get("/api/v1/reconciliation/alerts",
                   headers={"Authorization": f"Bearer {tok_sup}"})
    assert r.status_code == 200 and "items" in r.json()