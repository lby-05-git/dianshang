# -*- coding: utf-8 -*-
"""Phase 1 退款状态独立事件流（-1）：refund_events 幂等 + 查询/重试/确认。

覆盖：
- refund_status 查询返回事件轨迹（含 previous/new/guid/gateway_before）；
- 事件 guid 幂等：重放相同 guid 不插入重复审计；
- retry 不改变退款状态（状态机不回滚）；
- 主管 confirm APPROVED -> SUCCESS / REJECTED -> FAILED；
- confirm SUPPLEMENT 只留审计不改状态；
- 非法状态 confirm 拒绝。
"""
import pytest
import uuid

from app.domain.models import (
    REFUND_STATUS_FAILED,
    REFUND_STATUS_NONE,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    RefundEvent,
)
from app.services.refund_events import (
    confirm_refund_event,
    query_refund_status,
    record_refund_event,
    retry_refund,
)
from tests.conftest import SessionTest


def _submitter_id(db) -> str:
    from app.domain.models import User

    u = db.query(User).filter_by(username="customer_service1").first()
    return u.id if u else "seed-missing"


def _case(db) -> RefundCase:
    from decimal import Decimal

    from app.domain.models import CASE_STATUS_COMPLETED, CHANNEL_DEFAULT, DECISION_APPROVED

    case = RefundCase(
        id=str(uuid.uuid4()), order_id=f"ORD-EV-{uuid.uuid4().hex[:8]}",
        order_no="NO-EV", channel=CHANNEL_DEFAULT, requested_amount=Decimal("128.00"),
        refund_reason="测试退款事件", current_status=CASE_STATUS_COMPLETED,
        final_decision=DECISION_APPROVED, submitter_id=_submitter_id(db),
    )
    db.add(case)
    db.commit()
    return case


def test_refund_status_query_empty(client):
    s = SessionTest()
    try:
        c = _case(s)
        out = query_refund_status(s, c.id)
        assert out["case_id"] == c.id
        assert out["refund_status"] == REFUND_STATUS_NONE
        assert out["events"] == []
    finally:
        s.close()


def test_refund_event_idempotent_guid(client):
    s = SessionTest()
    try:
        c = _case(s)
        guid = f"re-test-{uuid.uuid4().hex}"
        e1 = record_refund_event(
            s, case_id=c.id, event_type="EXEC", previous_status=REFUND_STATUS_NONE,
            new_status=REFUND_STATUS_PENDING, guid=guid,
        )
        s.refresh(c)
        assert c.refund_events_guid == guid
        e2 = record_refund_event(
            s, case_id=c.id, event_type="OK", previous_status=REFUND_STATUS_PENDING,
            new_status=REFUND_STATUS_SUCCESS, guid=guid,
        )
        # 同 guid 幂等：返回同一事件行，不重复插入
        assert e1.id == e2.id
        cnt = s.query(RefundEvent).filter_by(guid=guid).count()
        assert cnt == 1
    finally:
        s.close()


def test_refund_retry_does_not_change_status(client):
    s = SessionTest()
    try:
        c = _case(s)
        record_refund_event(s, case_id=c.id, event_type="EXEC",
                            previous_status=REFUND_STATUS_NONE,
                            new_status=REFUND_STATUS_PENDING)
        e = retry_refund(s, c.id)
        assert e["refund_status"] == REFUND_STATUS_PENDING   # 状态机不回滚
        assert e["replayed"] is True
    finally:
        s.close()


def test_confirm_approved_to_success(client):
    s = SessionTest()
    try:
        c = _case(s)
        case = s.get(RefundCase, c.id)
        case.refund_status = REFUND_STATUS_PENDING
        s.commit()
        evt = confirm_refund_event(s, c.id, new_status=REFUND_STATUS_SUCCESS,
                                   opinion="主管确认到账", confirmed_by="supervisor1")
        assert evt.new_status == REFUND_STATUS_SUCCESS
        s.refresh(case)
        assert case.refund_status == REFUND_STATUS_SUCCESS
        out = query_refund_status(s, c.id)
        assert out["refund_status"] == REFUND_STATUS_SUCCESS
        assert any(e["event_type"] == "CONFIRM" for e in out["events"])
    finally:
        s.close()


def test_confirm_rejected_to_failed(client):
    s = SessionTest()
    try:
        c = _case(s)
        evt = confirm_refund_event(s, c.id, new_status=REFUND_STATUS_FAILED,
                                   opinion="驳回", confirmed_by="supervisor1")
        assert evt.new_status == REFUND_STATUS_FAILED
    finally:
        s.close()


def test_confirm_supplement_keeps_status(client):
    s = SessionTest()
    try:
        c = _case(s)
        case = s.get(RefundCase, c.id)
        case.refund_status = REFUND_STATUS_PENDING
        s.commit()
        evt = confirm_refund_event(s, c.id, new_status=REFUND_STATUS_PENDING,
                                   opinion="补充材料", confirmed_by="supervisor1")
        assert evt.event_type == "CONFIRM"
        s.refresh(case)
        assert case.refund_status == REFUND_STATUS_PENDING   # 补充不改状态
    finally:
        s.close()


def test_confirm_invalid_status_rejected(client):
    s = SessionTest()
    try:
        c = _case(s)
        with pytest.raises(ValueError):
            confirm_refund_event(s, c.id, new_status="BOGUS", opinion="x")
    finally:
        s.close()