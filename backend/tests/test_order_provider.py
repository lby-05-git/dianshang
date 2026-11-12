# -*- coding: utf-8 -*-
"""Mock 订单 Provider 单元测试（数据源）。

只测 provider 逻辑，不碰数据库 —— 确保订单真实性/归属/可退资格/超时 三类边界。
"""
import pytest

from app.infrastructure.order_provider import (
    MATCH,
    MISMATCH,
    UNKNOWN,
    MockOrderProvider,
    ProviderResult,
    verify_order,
)


class _NoTimeoutProvider(MockOrderProvider):
    """禁掉全局超时注入（保持用例确定性）。"""

    def __init__(self):
        super().__init__(timeout_for_username=None, base_delay=0.0)


@pytest.mark.parametrize("order_id", ["ORD-1001", "ORD-2001", "ORD-2002", "ORD-3001", "ORD-3002", "ORD-3003"])
def test_builtin_orders_exist(order_id):
    pr = _NoTimeoutProvider().lookup(order_id)
    assert pr.ok
    assert pr.order is not None


def test_order_not_found_is_mismatch():
    res = verify_order(_NoTimeoutProvider(), "ORD-NOT-EXIST", "customer_service1")
    assert res["order_verification"] == MISMATCH
    assert any("不存在" in r for r in res["reasons"])


def test_ownership_match():
    res = verify_order(_NoTimeoutProvider(), "ORD-1001", "customer_service1")
    assert res["order_verification"] == MATCH


def test_ownership_mismatch_suspends():
    # ORD-3001 属于 supervisor1，customer_service1 申请 -> MISMATCH
    res = verify_order(_NoTimeoutProvider(), "ORD-3001", "customer_service1")
    assert res["order_verification"] == MISMATCH
    assert any("归属" in r for r in res["reasons"])


def test_provider_timeout_unknown():
    # 传入 timeout_for_username 使 ORD-TIMEOUT-* 超时 -> UNKNOWN（不能都是 MISMATCH）
    prov = MockOrderProvider(timeout_for_username="someone")
    res = verify_order(prov, "ORD-TIMEOUT-1", "customer_service1")
    assert res["order_verification"] == UNKNOWN
    assert res["order_status"] is None
    assert any("超时" in r for r in res["reasons"])


def test_no_refundable_balance_mismatch():
    # ORD-3003 可退金额=300，若申请额>可退额，由价格核验节点负责；此处确认订单本身存在
    prov = _NoTimeoutProvider()
    res = verify_order(prov, "ORD-3003", "customer_service1")
    assert res["order_verification"] == MATCH
    assert res["refundable_amount_yuan"] == 300.0


def test_provider_result_defaults_are_safe():
    # 空 ProviderResult 默认不 ok、不超时、不视为"可用假结果"
    r = ProviderResult()
    assert r.ok is False
    assert r.order is None
    assert r.error == ""