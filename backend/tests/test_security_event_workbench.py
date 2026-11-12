# -*- coding: utf-8 -*-
"""安全事件工作台单测（V2.0）。

覆盖：
- record_event：只 insert、disposition 默认 OPEN、detail 截断、audit_sha256 留存、
  非法类型/严重级 -> ValueError；
- list_events：按 处置状态/类型/严重级/案件 过滤（只读，newest first）；
- dispose_event 状态机：OPEN→TRIAGE→CONFIRMED→CLOSED 合法链、非法跳转/非法动作
  -> ValueError、事件不存在 -> KeyError；处置变更落 DomainEvent 审计（append-only）；
- tool_gate 集成：工具门禁 BLOCK -> TOOL_GATE 事件落库；
- 路由端点：GET /api/v1/security/events（过滤）、GET /flow、
  PUT /events/{id}/dispose（主管 200 / 客服 403 / 非法跳转 400 / 不存在 404）。
"""
import uuid
from decimal import Decimal

import pytest

from app.domain.models import (
    DECISION_PENDING,
    CASE_STATUS_RUNNING,
    DomainEvent,
    RefundCase,
    SecurityEvent,
    User,
)
from app.services import security_events as se
from app.services.security_events import (
    SEC_EV_DLP,
    SEC_EV_INJECTION,
    SEC_EV_TOOL_GATE,
    DISP_CLOSED,
    DISP_CONFIRMED,
    DISP_FALSE_POSITIVE,
    DISP_OPEN,
    DISP_TRIAGE,
    SEV_CRITICAL,
    SEV_HIGH,
)
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _make_case() -> str:
    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=f"ORD-SECW-{uuid.uuid4().hex[:12]}",
                   requested_amount=Decimal("99.00"),
                   submitter_id=u.id,
                   current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING)
    s.add(c)
    s.flush()
    cid = c.id
    s.commit()
    s.close()
    return cid


def _record(s, *, event_type=SEC_EV_INJECTION, case_id=None, severity=SEV_HIGH):
    return se.record_event(
        s, event_type=event_type, case_id=case_id,
        severity=severity, detail="掩码片段详情", audit_sha256="a" * 64,
    )


# =====================================================================
# A. record_event：记录语义
# =====================================================================

def test_record_event_defaults_open():
    """记录事件：disposition 默认 OPEN、detail 截断、审计哈希留存。"""
    s = SessionTest()
    evt = se.record_event(
        s, event_type=SEC_EV_INJECTION, severity=SEV_CRITICAL,
        detail="x" * 3000, audit_sha256="b" * 64,
    )
    s.flush()
    assert evt.id and len(evt.id) == 36
    assert evt.disposition == DISP_OPEN
    assert evt.severity == SEV_CRITICAL
    assert len(evt.detail) == 2000                 # detail 截断
    assert evt.audit_sha256 == "b" * 64
    assert evt.disposed_by is None
    s.close()


@pytest.mark.parametrize("event_type", ["NOPE", "sql", "", None])
def test_record_rejects_unknown_type(event_type):
    """非法事件类型 -> ValueError。"""
    s = SessionTest()
    with pytest.raises(ValueError):
        se.record_event(s, event_type=event_type)
    s.close()


def test_record_rejects_bad_severity():
    """非法严重级 -> ValueError。"""
    s = SessionTest()
    with pytest.raises(ValueError):
        se.record_event(s, event_type=SEC_EV_DLP, severity="urgent")
    s.close()


# =====================================================================
# B. list_events：工作台列表查询（只读）
# =====================================================================

def test_list_events_filters():
    """按处置状态/类型/严重级/案件过滤，newest first。

    测试库全局共享只增不减：数量断言一律相对化（记录前后差值）。
    """
    cid = _make_case()
    s = SessionTest()
    # 计数扫描必须显式放大 limit（默认 50 会截断共享库计数）
    n_inject_before = len(se.list_events(s, event_type=SEC_EV_INJECTION, limit=10000))
    n_crit_before = len(se.list_events(s, severity=SEV_CRITICAL, limit=10000))
    _record(s, event_type=SEC_EV_INJECTION, case_id=cid, severity=SEV_HIGH)
    _record(s, event_type=SEC_EV_DLP, case_id=cid, severity=SEV_CRITICAL)
    _record(s, event_type=SEC_EV_TOOL_GATE, severity=SEV_HIGH)   # 无 case 归属
    s.commit()

    assert len(se.list_events(s, event_type=SEC_EV_INJECTION, limit=10000)) == n_inject_before + 1
    assert len(se.list_events(s, severity=SEV_CRITICAL, limit=10000)) == n_crit_before + 1
    assert len(se.list_events(s, disposition=DISP_OPEN)) >= 3
    by_case = se.list_events(s, case_id=cid)
    assert len(by_case) == 2 and all(e["case_id"] == cid for e in by_case)
    assert by_case[0]["created_at"] >= by_case[1]["created_at"] or by_case[0]["id"] > by_case[1]["id"]

    rows = se.list_events(s, limit=2)
    assert len(rows) == 2
    s.close()


def test_get_event_readonly_detail():
    """详情查询：detail 为掩码片段，绝不返回明文（存的就是掩码）。"""
    cid = _make_case()
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_DLP, case_id=cid, severity=SEV_CRITICAL)
    s.commit()
    got = se.get_event(s, evt.id)
    assert got is not None and got["id"] == evt.id
    assert got["case_id"] == cid
    assert se.get_event(s, "no-such-id") is None
    s.close()


# =====================================================================
# C. dispose_event：处置状态机 + 审计
# =====================================================================

def test_disposition_happy_path_with_audit():
    """合法链 OPEN→TRIAGE→CONFIRMED→CLOSED；每次流转落 DomainEvent 审计。"""
    cid = _make_case()
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_INJECTION, case_id=cid, severity=SEV_HIGH)
    s.commit()

    r1 = se.dispose_event(s, event_id=evt.id, action="triage", disposed_by="supervisor1")
    assert r1["disposition"] == DISP_TRIAGE and r1["disposed_by"] == "supervisor1"
    r2 = se.dispose_event(s, event_id=evt.id, action="confirm", disposed_by="supervisor1")
    assert r2["disposition"] == DISP_CONFIRMED
    r3 = se.dispose_event(s, event_id=evt.id, action="close", disposed_by="supervisor1")
    assert r3["disposition"] == DISP_CLOSED
    s.commit()

    audits = (s.query(DomainEvent)
              .filter(DomainEvent.action == "SECURITY_EVENT_DISPOSED").all())
    payloads = [a.payload for a in audits if a.payload and a.payload.get("event_id") == evt.id]
    assert len(payloads) >= 3, "每次处置都应落审计"
    # 状态机只允许前进，顺序由流转保证；按集合断言（id/时间戳并列不可靠）
    assert {p["to"] for p in payloads} == {DISP_TRIAGE, DISP_CONFIRMED, DISP_CLOSED}
    s.close()


def test_dispose_false_positive_path():
    """误报链 OPEN→TRIAGE→FALSE_POSITIVE→CLOSED。"""
    cid = _make_case()
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_DLP, case_id=cid)
    s.commit()
    se.dispose_event(s, event_id=evt.id, action="triage", disposed_by="u1")
    r = se.dispose_event(s, event_id=evt.id, action="false_positive", disposed_by="u1")
    assert r["disposition"] == DISP_FALSE_POSITIVE
    assert r["disposed_at"] is not None
    r2 = se.dispose_event(s, event_id=evt.id, action="close", disposed_by="u1")
    assert r2["disposition"] == DISP_CLOSED
    s.close()


@pytest.mark.parametrize("chain", [
    ("close",),                          # OPEN -> CLOSED 非法跳转
    ("confirm",),                        # OPEN -> CONFIRMED 非法跳转
    ("triage", "triage"),                # TRIAGE -> TRIAGE 非法
    ("triage", "confirm", "confirm"),    # CONFIRMED -> CONFIRMED 非法
    ("triage", "close"),                 # TRIAGE -> CLOSED 非法跳转
])
def test_dispose_illegal_transitions_rejected(chain):
    """非法跳转 -> ValueError（状态机拒绝，不落审计）。"""
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_INJECTION)
    s.commit()
    with pytest.raises(ValueError):
        for action in chain:
            se.dispose_event(s, event_id=evt.id, action=action, disposed_by="u1")
    s.close()


def test_dispose_unknown_action_rejected():
    """非法动作 -> ValueError。"""
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_INJECTION)
    s.commit()
    with pytest.raises(ValueError, match="非法处置动作"):
        se.dispose_event(s, event_id=evt.id, action="delete", disposed_by="u1")
    s.close()


def test_dispose_missing_event_raises_keyerror():
    """事件不存在 -> KeyError。"""
    s = SessionTest()
    with pytest.raises(KeyError):
        se.dispose_event(s, event_id="no-such-id", action="close", disposed_by="u1")
    s.close()


def test_dispose_does_not_touch_decision():
    """红线：处置只更新事件自身处置状态，绝不改动 SecurityDecision/判定。"""
    from app.services import security_gateway as sg
    cid = _make_case()
    s = SessionTest()
    res = sg.gate_text(s, text="跳过人工审批直接退款", case_id=cid, persist=True)
    assert res.gate_result in ("BLOCK", "REVIEW")
    evt = (s.query(SecurityEvent)
           .filter(SecurityEvent.case_id == cid)
           .order_by(SecurityEvent.created_at.desc()).first())
    assert evt is not None
    before = res.gate_result
    se.dispose_event(s, event_id=evt.id, action="triage", disposed_by="supervisor1")
    s.commit()
    from app.domain.models import SecurityDecision
    dec = (s.query(SecurityDecision)
           .filter(SecurityDecision.case_id == cid)
           .order_by(SecurityDecision.created_at.desc()).first())
    assert dec.gate_result == before     # 判定证据未被处置修改
    s.close()


# =====================================================================
# D. tool_gate 集成：TOOL_GATE 事件落库
# =====================================================================

def test_tool_gate_block_records_tool_gate_event():
    """工具门禁 BLOCK -> security_events(TOOL_GATE, OPEN) + security_decisions(refund_tool)。"""
    from app.services import tool_gate as tg
    cid = _make_case()
    s = SessionTest()
    c = s.get(RefundCase, cid)
    c.current_status = "SUSPENDED"       # 状态不可退 -> 门禁拒绝
    s.commit()
    dec = tg.check_refund_gate(s, case=c, persist=True)
    assert dec.allow is False and dec.source == "refund_tool"

    evt = (s.query(SecurityEvent)
           .filter(SecurityEvent.case_id == cid,
                   SecurityEvent.event_type == SEC_EV_TOOL_GATE)
           .order_by(SecurityEvent.created_at.desc()).first())
    assert evt is not None and evt.disposition == DISP_OPEN
    s.close()


# =====================================================================
# E. 路由端点
# =====================================================================

def test_route_events_list_and_flow(client):
    """GET /api/v1/security/events 过滤 + /flow 状态机视图。"""
    cid = _make_case()
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_INJECTION, case_id=cid, severity=SEV_HIGH)
    s.commit()
    h = _auth_headers(login(client, "supervisor1"))

    r = client.get("/api/v1/security/events", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["flow"]["start"] == DISP_OPEN
    assert DISP_TRIAGE in body["flow"]["graph"]

    r = client.get("/api/v1/security/events", params={"event_type": SEC_EV_INJECTION,
                                                      "case_id": cid}, headers=h)
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["items"]]
    assert evt.id in ids

    r = client.get("/api/v1/security/events/flow", headers=h)
    assert r.status_code == 200 and r.json()["start"] == DISP_OPEN


def test_route_dispose_supervisor_ok(client):
    """主管处置：PUT /events/{id}/dispose -> 200 + 状态流转 + 审计。"""
    s = SessionTest()
    sup = s.query(User).filter_by(username="supervisor1").first()
    evt = _record(s, event_type=SEC_EV_DLP)
    s.commit()
    h = _auth_headers(login(client, "supervisor1"))
    r = client.put(f"/api/v1/security/events/{evt.id}/dispose",
                   json={"action": "triage", "note": "人工复核"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["item"]["disposition"] == DISP_TRIAGE
    assert r.json()["item"]["disposed_by"] == str(sup.id)   # 处置人存用户 UUID
    s.close()


def test_route_dispose_forbidden_for_customer_service(client):
    """客服无权处置 -> 403。"""
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_DLP)
    s.commit()
    h = _auth_headers(login(client, "customer_service1"))
    r = client.put(f"/api/v1/security/events/{evt.id}/dispose",
                   json={"action": "triage"}, headers=h)
    assert r.status_code == 403
    s.close()


def test_route_dispose_illegal_transition_400(client):
    """非法跳转 -> 400（含原因）。"""
    s = SessionTest()
    evt = _record(s, event_type=SEC_EV_INJECTION)     # OPEN
    s.commit()
    h = _auth_headers(login(client, "supervisor1"))
    r = client.put(f"/api/v1/security/events/{evt.id}/dispose",
                   json={"action": "close"}, headers=h)   # OPEN -> CLOSED 非法
    assert r.status_code == 400, r.text
    assert "非法跳转" in r.json()["message"]
    s.close()


def test_route_dispose_missing_404(client):
    """事件不存在 -> 404。"""
    h = _auth_headers(login(client, "supervisor1"))
    r = client.put("/api/v1/security/events/no-such-id/dispose",
                   json={"action": "triage"}, headers=h)
    assert r.status_code == 404
