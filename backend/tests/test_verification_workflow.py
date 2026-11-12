# -*- coding: utf-8 -*-
"""M3 订单/商品/价格核验节点工作流测试。

断言核验结果写回 RefundCase 列、MISMATCH/UNKNOWN -> 强挂起不自动退款、
全 MATCH 低风险仍秒退。使用内置确定订单（customer_service1）。
"""
import uuid
from decimal import Decimal

import pytest

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    Evidence,
    RefundCase,
    User,
)
from tests.conftest import SessionTest


@pytest.fixture()
def env(monkeypatch):
    """测试环境：Fake OCR/LLM + 测试库会话注入工作流模块。"""
    from app.infrastructure import llm, ocr, refund, vision
    from app.infrastructure.order_provider import MockOrderProvider
    from app.workflow import graph, runner

    monkeypatch.setattr(graph, "SessionLocal", SessionTest)
    monkeypatch.setattr(runner, "SessionLocal", SessionTest)
    monkeypatch.setattr(ocr, "get_ocr_provider",
                        lambda: ocr.FakeOcrProvider(confidence=0.95))
    monkeypatch.setattr(vision, "get_vision_provider", lambda: vision.FakeVisionProvider())
    monkeypatch.setattr(llm, "get_sentiment_provider",
                        lambda: llm.FakeSentimentProvider(level="LOW"))
    monkeypatch.setattr(refund, "MockRefundProvider", refund.MockRefundProvider)
    # 订单核验：图节点使用测试 provider
    monkeypatch.setattr(graph, "get_order_provider", lambda: _provider())
    return {"graph": graph, "runner": runner}


_PROVIDER_NS = {"p": None}


def _provider():
    from app.infrastructure.order_provider import MockOrderProvider
    if _PROVIDER_NS["p"] is None:
        _PROVIDER_NS["p"] = MockOrderProvider(timeout_for_username=None)
    return _PROVIDER_NS["p"]


def _register_prebuilt(provider, order_id: str, owner: str):
    """把内置订单注册进测试 provider（供归属 MISMATCH / 商品/价格用例固定引用）。"""
    from app.infrastructure.order_provider import _BUILTIN_ORDERS

    rec = _BUILTIN_ORDERS[order_id]
    provider.register_order(rec)


def _make_case(amount: str, order_id: str | None = None, reason: str | None = None,
               with_evidence: bool = True, as_user: str | None = None) -> str:
    """造 case。order_id 为 None 时注册唯一随机订单（owner=customer_service1），
    免疫 fraud 历史串扰：fraud 规则 2/3 按 order_id 统计历史案件（无 created_at
    过滤），测试库数据永续累积，任何重复 order_id 都会破坏 auto-approve 语义。
    内置订单用例显式传 order_id（这些用例本就要验证 MISMATCH）。"""
    from app.infrastructure.order_provider import _BUILTIN_ORDERS, OrderItem, OrderRecord

    provider = _provider()
    owner = "customer_service1"
    if order_id is not None and order_id.startswith("ORD-"):
        # 内置订单引用（ORD-1001/3001/3002/3003...）
        if order_id in _BUILTIN_ORDERS:
            _register_prebuilt(provider, order_id, owner)
        # 超时订单：测试专门的 provider 已经由 timeout_for 处理，直接透传
    else:
        oid = order_id or f"ORD-V-{uuid.uuid4().hex[:12]}"
        provider.register_order(OrderRecord(
            order_id=oid, order_no=f"NO-{uuid.uuid4().hex[:8]}", user_username=owner,
            status="PAID", total_price_yuan=float(amount), paid_amount_yuan=float(amount),
            refunded_amount_yuan=0.0, refundable_amount_yuan=float(amount),
            items=[OrderItem(sku=f"SKU-V-{uuid.uuid4().hex[:4]}", name="保温杯", spec="500ml", quantity=1)],
        ))
        order_id = oid

    username = as_user or owner
    s = SessionTest()
    u = s.query(User).filter_by(username=username).first()
    if u is None:
        u = User(username=username, password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=order_id, requested_amount=Decimal(amount),
                   submitter_id=u.id, current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING, refund_reason=reason)
    s.add(c)
    s.flush()
    if with_evidence:
        s.add(Evidence(case_id=c.id, file_url="uploads/fake.jpg"))
    s.commit()
    s.close()
    return c.id


def _case(cid: str) -> RefundCase:
    s = SessionTest()
    c = s.get(RefundCase, cid)
    s.close()
    return c


def test_match_low_amount_auto_approve(env):
    """99 元全可退（唯一订单，无历史） + MATCH 核验 + 低风险 -> 自动秒退 COMPLETED+APPROVED。

    秒退用例必须用唯一订单而不是内置 ORD-1001：fraud 规则 2/3 按 order_id 统计
    历史案件（≥3 次 / 24h → +25，历史≥2 → +20），跨测试运行累积会破坏秒退语义；
    唯一订单天然无历史，免疫该串扰（与 test_workflow.py 的 _make_case 一致）。
    """
    cid = _make_case("99.00", None, "保温杯破损申请退款")
    assert env["runner"].WorkflowRunner().start(cid) == "COMPLETED"
    c = _case(cid)
    assert (c.current_status, c.final_decision) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)
    assert c.order_verification == "MATCH"
    assert c.product_verification == "MATCH"
    assert c.price_verification == "MATCH"


def test_order_not_found_suspends(env):
    """订单不存在 -> 核验 MISMATCH -> 挂起不退款。"""
    cid = _make_case("99.00", "ORD-NOT-EXIST", "请求退款")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"
    c = _case(cid)
    assert (c.current_status, c.final_decision) == (CASE_STATUS_SUSPENDED, DECISION_PENDING)
    assert c.order_verification in ("MISMATCH",)
    assert c.product_verification == "UNKNOWN"
    assert c.price_verification == "UNKNOWN"


def test_order_owner_mismatch_suspends(env):
    """订单属于他人（ORD-3001 属 supervisor1）-> 归属 MISMATCH -> 挂起。"""
    cid = _make_case("99.00", "ORD-3001", "机械键盘申请退款", as_user="customer_service1")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"
    c = _case(cid)
    assert c.order_verification == "MISMATCH"


def test_product_mismatch_suspends(env, monkeypatch):
    """reason 声称"不是这个商品" -> 商品 MISMATCH -> 挂起。"""
    from app.workflow import graph
    from app.infrastructure import order_provider

    # ORD-3002 为蓝牙音箱(黑色)，reason 声称不符 -> 商品 MISMATCH
    cid = _make_case("120.00", "ORD-3002", "这个不是我买的商品，与我订单不符")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"
    c = _case(cid)
    assert c.product_verification == "MISMATCH"


def test_amount_above_refundable_suspends(env):
    """申请金额 > 可退金额（ORD-3003 可退 300，申请 500）-> 价格 MISMATCH -> 挂起。"""
    cid = _make_case("500.00", "ORD-3003", "冰箱申请退款")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"
    c = _case(cid)
    assert c.price_verification == "MISMATCH"


def test_provider_timeout_unknown_suspends(env, monkeypatch):
    """订单 Provider 超时 -> 核验 UNKNOWN -> 挂起（不伪 pass）。"""
    from app.workflow import graph

    monkeypatch.setattr(
        graph,
        "run_verification",
        lambda **kw: _fake_verification(all=("UNKNOWN", "UNKNOWN", "UNKNOWN"),
                                        reasons=["订单源超时"]),
    )
    cid = _make_case("99.00", "ORD-TIMEOUT-1", "退款")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"


def test_reject_suspends_no_refund(env):
    """价格 MISMATCH 即使审批也不得自动通过：最终由主管驳回，退款 NONE。"""
    cid = _make_case("500.00", "ORD-3003", "冰箱申请退款")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"
    # 主管拒绝 -> COMPLETED/REJECTED，且无退款回执（核查 refund_status NONE）
    assert runner.resume(cid, "REJECTED", "金额超可退范围") == "COMPLETED"
    c = _case(cid)
    assert (c.current_status, c.final_decision) == (CASE_STATUS_COMPLETED, "REJECTED")
    assert c.refund_status == "NONE"


def _fake_verification(**kw) -> object:
    """构造假 VerificationOutcome，用于注入超时场景。"""
    from app.workflow.verification import VerificationOutcome

    o = VerificationOutcome()
    o.order_verification, o.product_verification, o.price_verification = kw["all"]
    o.order_reasons = kw.get("reasons") or []
    o.product_reasons = list(kw.get("reasons") or [])
    o.price_reasons = list(kw.get("reasons") or [])
    return o
