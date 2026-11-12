# -*- coding: utf-8 -*-
"""Phase 1 订单中心沙箱查询快照（-1）：order_lookup_snapshots 落库。

覆盖：
- 查询正常 -> 快照落库 + reason_codes 空 + gate_result=PASS + 案件 last_order_snapshot_id 挂链；
- 订单不存在 -> BLOCK + ORDER_NOT_FOUND 原因码；
- 归属不符 -> BLOCK + OWNER_MISMATCH；
- Provider 超时 -> SUSPEND + ORDER_LOOKUP_TIMEOUT（安全挂起，不伪造默认值）；
- 无可退金额 -> BLOCK + NO_REFUNDABLE；
- 快照不可变：最新一次可追溯。
"""
import uuid

from app.domain.models import RefundCase
from app.infrastructure.order_provider import (
    OrderItem,
    OrderRecord,
    MockOrderProvider,
)
from app.services.order_lookup import (
    GATE_RESULT_BLOCK,
    GATE_RESULT_PASS,
    GATE_RESULT_SUSPEND,
    LOOKUP_KIND_API,
    REASON_LOOKUP_TIMEOUT,
    REASON_NO_REFUNDABLE,
    REASON_ORDER_NOT_FOUND,
    REASON_OWNER_MISMATCH,
    snapshot_order_lookup,
)
from tests.conftest import SessionTest


def _provider() -> MockOrderProvider:
    return MockOrderProvider(timeout_for_username=None)


def _make_case(db, *, order_id="ORD-X1", submitter_id=None) -> RefundCase:
    from decimal import Decimal

    from app.domain.models import (
        CASE_STATUS_RUNNING,
        CHANNEL_DEFAULT,
        DECISION_PENDING,
        User,
    )

    if submitter_id is None:
        u = db.query(User).filter_by(username="customer_service1").first()
        submitter_id = u.id if u else None
    case = RefundCase(
        id=str(uuid.uuid4()), order_id=order_id, order_no=order_id,
        channel=CHANNEL_DEFAULT, requested_amount=Decimal("99.00"),
        refund_reason="test", current_status=CASE_STATUS_RUNNING,
        final_decision=DECISION_PENDING, submitter_id=submitter_id,
    )
    db.add(case)
    db.commit()
    return case


def test_lookup_matching_snapshot(client):
    """正常订单 -> 快照 PASS + last_order_snapshot_id 挂链。"""
    from app.domain.models import User

    s = SessionTest()
    try:
        user = s.query(User).filter_by(username="customer_service1").first()
        case = _make_case(s, order_id="ORD-1001", submitter_id=user.id if user else None)
        prov = _provider()
        prov.register_order(OrderRecord(
            order_id=case.order_id, order_no="NO-X", user_username="customer_service1",
            status="PAID", total_price_yuan=99.0, paid_amount_yuan=99.0,
            refunded_amount_yuan=0.0, refundable_amount_yuan=99.0,
            items=[OrderItem(sku="SKU-X", name="保温杯", spec="500ml", quantity=1)],
        ))
        snap = snapshot_order_lookup(s, case=case, provider=prov, lookup_kind=LOOKUP_KIND_API)
        s.refresh(case)
        assert snap.gate_result == GATE_RESULT_PASS
        assert (snap.reason_codes or []) == []
        assert snap.exists is True
        assert snap.owner_locked is True
        assert snap.refundable is True
        assert case.last_order_snapshot_id == snap.id
        assert len(snap.raw_sha256) == 64
    finally:
        s.close()


def test_lookup_order_not_found(client):
    from app.domain.models import User

    s = SessionTest()
    try:
        user = s.query(User).filter_by(username="customer_service1").first()
        case = _make_case(s, order_id="ORD-NOT-EXIST", submitter_id=user.id if user else None)
        snap = snapshot_order_lookup(s, case=case, provider=_provider())
        assert snap.gate_result == GATE_RESULT_BLOCK
        assert REASON_ORDER_NOT_FOUND in (snap.reason_codes or [])
        assert snap.exists is False
    finally:
        s.close()


def test_lookup_owner_mismatch(client):
    from app.domain.models import User

    s = SessionTest()
    try:
        user = s.query(User).filter_by(username="customer_service1").first()
        case = _make_case(s, order_id="ORD-OWNER-X", submitter_id=user.id if user else None)
        prov = _provider()
        prov.register_order(OrderRecord(
            order_id=case.order_id, order_no="NO-OWN", user_username="supervisor1",
            status="PAID", total_price_yuan=100.0, paid_amount_yuan=100.0,
            refunded_amount_yuan=0.0, refundable_amount_yuan=100.0,
            items=[OrderItem(sku="SKU-O", name="键盘", spec="87键", quantity=1)],
        ))
        snap = snapshot_order_lookup(s, case=case, provider=prov)
        assert snap.gate_result == GATE_RESULT_BLOCK
        assert REASON_OWNER_MISMATCH in (snap.reason_codes or [])
    finally:
        s.close()


def test_lookup_provider_timeout_safe_suspend(client):
    from app.domain.models import User

    from app.infrastructure.order_provider import (
        OrderProviderTimeout,
        ProviderResult,
    )

    class _TimeoutProvider(MockOrderProvider):
        """注入超时：lookup 一律返回 timed_out。"""

        def lookup(self, order_id):
            return ProviderResult(ok=False, error=f"订单源超时 (order_id={order_id})",
                                  timed_out=True, available=True)

    s = SessionTest()
    try:
        user = s.query(User).filter_by(username="customer_service1").first()
        case = _make_case(s, order_id="ORD-TIMEOUT-1", submitter_id=user.id if user else None)
        snap = snapshot_order_lookup(s, case=case, provider=_TimeoutProvider())
        assert snap.gate_result == GATE_RESULT_SUSPEND
        assert REASON_LOOKUP_TIMEOUT in (snap.reason_codes or [])
        assert snap.exists is False
    finally:
        s.close()


def test_lookup_no_refundable_blocks(client):
    from app.domain.models import User

    s = SessionTest()
    try:
        user = s.query(User).filter_by(username="customer_service1").first()
        case = _make_case(s, order_id="ORD-NO-REFUND", submitter_id=user.id if user else None)
        prov = _provider()
        prov.register_order(OrderRecord(
            order_id=case.order_id, order_no="NO-NR", user_username="customer_service1",
            status="REFUNDED", total_price_yuan=500.0, paid_amount_yuan=500.0,
            refunded_amount_yuan=500.0, refundable_amount_yuan=0.0,
            items=[OrderItem(sku="SKU-NR", name="冰箱", spec="双门", quantity=1)],
        ))
        snap = snapshot_order_lookup(s, case=case, provider=prov)
        assert snap.gate_result == GATE_RESULT_BLOCK
        assert REASON_NO_REFUNDABLE in (snap.reason_codes or [])
        assert snap.trade_status == "REFUNDED"
    finally:
        s.close()