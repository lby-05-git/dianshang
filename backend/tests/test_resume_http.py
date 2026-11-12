# -*- coding: utf-8 -*-
"""HTTP 挂起/恢复集成测试（基线 V1.0）。

覆盖：
- create -> checkpoint(SUSPENDED) -> resume(APPROVED) -> COMPLETED+APPROVED
  （用真 Redis Checkpointer，thread_id=case_id）；
- 挂起状态的 case 可经 HTTP resume 复用 runner.resume 恢复；
- 非挂起（RUNNING）case checkpoint 被拒（409）。

真实语义说明：resume 依赖真 Redis 检查点（安全约束：检查点缺失/损坏
一律拒绝恢复，从空态 start 属误恢复）。因此测试先跑真实图到 human_review
interrupt（350 元超阈值挂起，materialize 真实检查点），再经 HTTP checkpoint/
resume 走完整 语义。
"""
import uuid

import pytest

from app.domain.models import (
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    RefundCase,
)
from tests.conftest import SessionTest


@pytest.fixture()
def env(monkeypatch):
    """测试环境：让 HTTP resume 的 WorkflowRunner 走测试库会话。"""
    from app.infrastructure import llm, ocr, refund, vision
    from app.workflow import graph, runner

    monkeypatch.setattr(graph, "SessionLocal", SessionTest)
    monkeypatch.setattr(runner, "SessionLocal", SessionTest)
    monkeypatch.setattr(refund, "SessionLocal", SessionTest)  # 退款落库走测试库
    monkeypatch.setattr(ocr, "get_ocr_provider",
                        lambda: ocr.FakeOcrProvider(confidence=0.95))
    monkeypatch.setattr(vision, "get_vision_provider", lambda: vision.FakeVisionProvider())
    monkeypatch.setattr(llm, "get_sentiment_provider",
                        lambda: llm.FakeSentimentProvider(level="LOW"))
    monkeypatch.setattr(refund, "MockRefundProvider", refund.MockRefundProvider)

    # 订单核验：注册唯一测试订单（用户 customer_service1）
    monkeypatch.setattr(graph, "get_order_provider", lambda: _provider())
    return {"runner": runner}


_PROVIDER = None


def _provider():
    global _PROVIDER
    from app.infrastructure.order_provider import MockOrderProvider
    if _PROVIDER is None:
        _PROVIDER = MockOrderProvider(timeout_for_username=None)
    return _PROVIDER


def _register_order(amount: str) -> str:
    from app.infrastructure.order_provider import OrderItem, OrderRecord
    oid = f"ORD-HTTP-{uuid.uuid4().hex[:12]}"
    _provider().register_order(OrderRecord(
        order_id=oid, order_no=f"NO-{uuid.uuid4().hex[:8]}", user_username="customer_service1",
        status="PAID", total_price_yuan=float(amount), paid_amount_yuan=float(amount),
        refunded_amount_yuan=0.0, refundable_amount_yuan=float(amount),
        items=[OrderItem(sku=f"SKU-H-{uuid.uuid4().hex[:4]}", name="保温杯", spec="500ml", quantity=1)],
    ))
    return oid


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _create(client, token, amount="350.00"):
    """建单：直接落库 RUNNING + 返回 case_id（不走 XADD，避免 worker 异步竞态）。"""
    from decimal import Decimal

    cid = str(uuid.uuid4())
    order_id = _register_order(amount)
    from app.domain.models import (
        CASE_STATUS_RUNNING,
        DECISION_PENDING,
        RefundCase,
        User,
    )

    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(id=cid, order_id=order_id, requested_amount=Decimal(amount),
                   submitter_id=u.id, current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING, refund_reason="收到商品不符申请退款")
    s.add(c)
    s.commit()
    s.close()
    return cid


def _run_to_human_review(env, cid):
    """跑真实图到 human_review interrupt（350 元超阈值），materialize 真 Redis 检查点。"""
    runner = env["runner"].WorkflowRunner()
    outcome = runner.start(cid)
    assert outcome == "SUSPENDED"  # interrupt 挂起，检查点就位
    # start() 已把 case 落 SUSPENDED；断言真实检查点可用（resume 前置安全约束）
    assert runner._checkpoint_usable(cid)
    return runner


def test_http_checkpoint_then_resume(client, env):
    """create -> 挂起(真检查点) -> checkpoint(RUNNING 已被 start 置 SUSPENDED) -> resume(APPROVED)。"""
    token = _auth(client)
    cid = _create(client, token, amount="350.00")
    _run_to_human_review(env, cid)

    # case 已 SUSPENDED（start 挂起）；checkpoint 需 RUNNING 前置，这里直接验证 resume
    s = SessionTest()
    try:
        c = s.get(RefundCase, cid)
        assert c.current_status == CASE_STATUS_SUSPENDED
    finally:
        s.close()

    # HTTP resume（复用 runner.resume + 真 Redis Checkpointer）
    resp = client.post(f"/api/v1/refunds/{cid}/resume",
                       headers={"Authorization": f"Bearer {token}"},
                       data={"action": "APPROVED", "opinion": "核实无误批准"})
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["action"] == "APPROVED"
    assert payload["outcome"] == "COMPLETED"

    # resume 走真 Redis Checkpointer 后 case 进终态
    s = SessionTest()
    try:
        c = s.get(RefundCase, cid)
        assert c.final_decision == DECISION_APPROVED
    finally:
        s.close()


def test_checkpoint_running_case_then_resume_rejects_repeat(client, env):
    """RUNNING case checkpoint 成功 -> SUSPENDED；再 checkpoint（已 SUSPENDED）被 409。"""
    token = _auth(client)
    cid = _create(client, token, amount="128.00")

    r1 = client.post(f"/api/v1/refunds/{cid}/checkpoint",
                     headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 202

    s = SessionTest()
    try:
        c = s.get(RefundCase, cid)
        assert c.current_status == CASE_STATUS_SUSPENDED
    finally:
        s.close()

    # 已 SUSPENDED -> 再 checkpoint 409
    r2 = client.post(f"/api/v1/refunds/{cid}/checkpoint",
                     headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 409
