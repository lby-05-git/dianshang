# -*- coding: utf-8 -*-
"""商城订单同步的跨进程 Provider 回归测试（不依赖真实 Redis/MySQL）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.infrastructure import order_provider as op  # noqa: E402
from app.workflow.verification import run_verification  # noqa: E402


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def expire(self, key, seconds):
        return key in self.data


def _order(amount=99.0):
    return op.OrderRecord(
        order_id="ORD-EGET-REGRESSION-1",
        order_no="ORD-EGET-REGRESSION-1",
        user_username="customer_service1",
        status="PAID",
        total_price_yuan=amount,
        paid_amount_yuan=amount,
        refunded_amount_yuan=0.0,
        refundable_amount_yuan=amount,
        items=[op.OrderItem(sku="SKU-EGET-1", name="商城测试商品", spec="标准版", quantity=1)],
    )


def test_external_order_is_shared_and_verifiable(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(op, "redis_client", redis)

    assert op.register_external_order(_order()) is True
    assert op.register_external_order(_order()) is False  # 相同快照幂等

    provider = op.MockOrderProvider(timeout_for_username=None)
    outcome = run_verification(
        order_id="ORD-EGET-REGRESSION-1",
        requester_username="customer_service1",
        requested_amount_yuan=99.0,
        refund_reason="商城测试商品 SKU-EGET-1 申请退款",
        provider=provider,
    )
    assert outcome.order_verification == op.MATCH
    assert outcome.product_verification == op.MATCH
    assert outcome.price_verification == op.MATCH

    with pytest.raises(ValueError, match="不同的可信快照"):
        op.register_external_order(_order(amount=88.0))
