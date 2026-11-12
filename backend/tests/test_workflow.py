# -*- coding: utf-8 -*-
"""Phase 5 验证：LangGraph 四 Agent 工作流 + interrupt/resume 挂起唤醒。

覆盖：128 元自动秒退 / 350 元挂起转人工 / 审批恢复 / 拒绝 / OCR 失败强制人工 /
退款失败 SYSTEM_FAILURE / Output Parse 失败保守降级。
全部使用 Fake Provider（不依赖真实 OCR/LLM），DB 走测试库。
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
    DECISION_REJECTED,
    DECISION_SYSTEM_FAILURE,
    Evidence,
    RefundCase,
    User,
)
from tests.conftest import SessionTest


@pytest.fixture()
def env(monkeypatch):
    """测试环境：Fake OCR/LLM + 测试库会话注入工作流模块。"""
    from app.infrastructure import llm, ocr, refund, vision
    from app.workflow import graph, runner

    monkeypatch.setattr(graph, "SessionLocal", SessionTest)
    monkeypatch.setattr(runner, "SessionLocal", SessionTest)
    monkeypatch.setattr(refund, "SessionLocal", SessionTest)  # 退款落库走测试库（FK 一致）
    monkeypatch.setattr(ocr, "get_ocr_provider",
                        lambda: ocr.FakeOcrProvider(confidence=0.95))
    monkeypatch.setattr(vision, "get_vision_provider", lambda: vision.FakeVisionProvider())
    monkeypatch.setattr(llm, "get_sentiment_provider",
                        lambda: llm.FakeSentimentProvider(level="LOW"))
    monkeypatch.setattr(refund, "MockRefundProvider", refund.MockRefundProvider)
    # 订单核验：图节点使用测试 provider（含 _make_case 注册的唯一订单）
    monkeypatch.setattr(graph, "get_order_provider", lambda: _provider())
    return {"graph": graph, "runner": runner, "refund": refund}


_PROVIDER = None  # MockOrderProvider | None


def _provider():
    """全局测试 provider：注册唯一订单，供图节点 get_order_provider() 命中。"""
    global _PROVIDER
    from app.infrastructure.order_provider import MockOrderProvider
    if _PROVIDER is None:
        from app.infrastructure.order_provider import MockOrderProvider, OrderRecord, OrderItem
        _PROVIDER = MockOrderProvider(timeout_for_username=None)
    return _PROVIDER


def _make_case(amount: str, with_evidence: bool = True,
               order_id: str | None = None,
               refund_reason: str | None = "保温杯破损申请退款") -> str:
    """造 case。

    每个用例注册一张唯一 Mock 订单（owner=customer_service1），避免多个用例共用
    ORD-1001 触发 fraud 历史规则串扰、破坏既有秒退/挂起语义。核验结果为 MATCH。
    需要 MISMATCH/UNKNOWN 的边界由 verifications 测试覆盖。
    """
    from app.infrastructure.order_provider import OrderItem, OrderRecord

    provider = _provider()
    oid = order_id or f"ORD-T-{uuid.uuid4().hex[:12]}"
    provider.register_order(OrderRecord(
        order_id=oid, order_no=f"NO-{uuid.uuid4().hex[:8]}", user_username="customer_service1",
        status="PAID", total_price_yuan=float(amount), paid_amount_yuan=float(amount),
        refunded_amount_yuan=0.0, refundable_amount_yuan=float(amount),
        items=[OrderItem(sku=f"SKU-{uuid.uuid4().hex[:4]}", name="保温杯", spec="500ml", quantity=1)],
    ))
    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username=f"wf_{uuid.uuid4().hex[:8]}", password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=oid, requested_amount=Decimal(amount),
                   submitter_id=u.id, current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING, refund_reason=refund_reason)
    s.add(c)
    s.flush()
    if with_evidence:
        s.add(Evidence(case_id=c.id, file_url="uploads/fake.jpg"))
    s.commit()
    s.close()
    return c.id


def _case_state(cid: str) -> tuple[str, str]:
    s = SessionTest()
    c = s.get(RefundCase, cid)
    r = (c.current_status, c.final_decision)
    s.close()
    return r


def test_scenario2_auto_approve_128(env):
    """128 元 + 低风险 + OCR 正常 -> 全自动秒退 COMPLETED+APPROVED。"""
    cid = _make_case("128.00")
    outcome = env["runner"].WorkflowRunner().start(cid)
    assert outcome == "COMPLETED"
    assert _case_state(cid) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)


def test_scenario1_suspend_350_then_approve(env):
    """350 元 -> 挂起 SUSPENDED；主管 APPROVE -> 恢复图 -> 退款 -> COMPLETED+APPROVED。"""
    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    outcome = runner.start(cid)
    assert outcome == "SUSPENDED"
    assert _case_state(cid) == (CASE_STATUS_SUSPENDED, DECISION_PENDING)

    outcome2 = runner.resume(cid, "APPROVED", "情况属实，批准退款")
    assert outcome2 == "COMPLETED"
    assert _case_state(cid) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)


def test_suspend_then_reject(env):
    """主管 REJECT -> 恢复图 -> 拒绝终态（不执行退款）。"""
    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"
    assert runner.resume(cid, "REJECTED", "凭证不足") == "COMPLETED"
    assert _case_state(cid) == (CASE_STATUS_COMPLETED, DECISION_REJECTED)


def test_ocr_failure_forces_human_review(env, monkeypatch):
    """OCR 引擎异常（FAILED）-> 跳过 fraud/sentiment 直接挂起人工（docs/06 E-04）。"""
    from app.infrastructure import ocr

    class _BrokenOcr:
        def ocr(self, path):
            raise RuntimeError("ocr engine crashed")

    monkeypatch.setattr(ocr, "get_ocr_provider", lambda: _BrokenOcr())
    cid = _make_case("128.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"
    assert _case_state(cid) == (CASE_STATUS_SUSPENDED, DECISION_PENDING)


def test_ocr_low_confidence_forces_human_review(env, monkeypatch):
    """OCR 置信度 < 阈值 -> UNRELIABLE -> 强制人工（docs/06 E-05、手册 ）。"""
    from app.infrastructure import ocr

    monkeypatch.setattr(ocr, "get_ocr_provider",
                        lambda: ocr.FakeOcrProvider(confidence=0.3))
    cid = _make_case("128.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"
    assert _case_state(cid) == (CASE_STATUS_SUSPENDED, DECISION_PENDING)


def test_sentiment_unavailable_conservative(env, monkeypatch):
    """LLM 不可用（抛异常）-> 保守 HIGH -> 转人工（docs/07 手册 ）。"""
    from app.infrastructure import llm

    class _BrokenSentiment:
        def analyze(self, text):
            raise RuntimeError("llm down")

    monkeypatch.setattr(llm, "get_sentiment_provider", lambda: _BrokenSentiment())
    cid = _make_case("128.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"


def test_refund_failure_system_failure(env, monkeypatch):
    """退款执行失败 -> SYSTEM_FAILURE 终态（docs/06 E-15）。"""
    from app.infrastructure import refund

    _orig = refund.MockRefundProvider
    monkeypatch.setattr(refund, "MockRefundProvider", lambda: _orig(fail_once=True))
    cid = _make_case("128.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "COMPLETED"
    assert _case_state(cid) == (CASE_STATUS_COMPLETED, DECISION_SYSTEM_FAILURE)


def test_fraud_rule_evidence_reuse(env):
    """凭证复用规则：同 MD5 出现在其他案件 -> 欺诈分 +30 -> 128 元也转人工。"""
    cid1 = _make_case("128.00")
    cid2 = _make_case("128.00")
    s = SessionTest()
    ev1 = s.query(Evidence).filter_by(case_id=cid1).first()
    ev1.file_md5 = "deadbeef"
    ev2 = s.query(Evidence).filter_by(case_id=cid2).first()
    ev2.file_md5 = "deadbeef"
    s.commit()
    s.close()

    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid2) == "SUSPENDED"  # 复用 +30 -> 30 分 > 20 -> 保守人工


def test_visual_claim_contradiction_adds_risk_and_suspends(env, monkeypatch):
    """Qwen-VL 判定图文矛盾且凭证无关 -> +60，低金额也必须挂起。"""
    from app.infrastructure import vision

    result = vision.VisionAnalysisResult(
        evidence_type="product_photo",
        visible_text=vision.VisionVisibleText(product_name="运动鞋"),
        visual_facts=["图片展示一双运动鞋，未见保温杯"],
        damage=vision.DamageAnalysis(
            detected=False, category="NONE", type=None, severity="none"
        ),
        image_quality=vision.ImageQuality(clear=True, occluded=False),
        claim_alignment=vision.ClaimAlignment(
            verdict="CONTRADICTED", reason="图片商品与退款说明不一致", evidence_relevant=False,
        ),
        confidence=0.92,
    )
    monkeypatch.setattr(
        vision, "get_vision_provider", lambda: vision.FakeVisionProvider(result=result)
    )
    cid = _make_case("128.00", refund_reason="保温杯破损申请退款")
    assert env["runner"].WorkflowRunner().start(cid) == "SUSPENDED"

    s = SessionTest()
    try:
        case = s.get(RefundCase, cid)
        # 图文矛盾 +30、凭证不相关 +30、视觉商品与可信订单不一致 +35。
        assert case.fraud_score == 95
        assert case.ocr_summary["claim_alignment"]["verdict"] == "CONTRADICTED"
    finally:
        s.close()


# ---- 挂起前检查点快照包含策略版本/挂起原因/待审批信息 ----

def _ckpt_state(cid: str) -> dict:
    """从 Redis 读回案件 latest 检查点的 channel_values（langgraph:checkpoint:{cid}）。

    写入走 checkpointer 的二进制连接（decode_responses=False，pickle 原字节入库）；
    这里必须用同一连接读回，否则 redis-py 会把 0x80 协议字节按 UTF-8 解码直接
    UnicodeDecodeError。
    """
    import pickle as _pickle

    from app.workflow.redis_checkpointer import CKPT_PREFIX, _binary_redis
    key = f"{CKPT_PREFIX}{cid}"
    latest = _binary_redis.hget(key, "latest")
    if not latest:
        return {}
    raw = _binary_redis.hget(key, latest)
    if not raw:
        return {}
    checkpoint, _ = _pickle.loads(raw)
    return checkpoint.get("channel_values") or {}


def test_suspend_checkpoint_has_f15b13_fields(env):
    """挂起（350 元 -> 人工）后检查点快照须含 policy_version/suspend_reason/pending_approval。"""
    cid = _make_case("350.00")   # 超阈值 -> HUMAN_REVIEW -> 挂起
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"

    st = _ckpt_state(cid)
    assert st.get("suspend_reason"), f"检查点缺 suspend_reason: {st}"
    assert "金额" in st["suspend_reason"] or "超阈值" in st["suspend_reason"]
    # 策略档 meta：Profile 存在则带 id/name；无档（MVP 兜底）也须非 None 或显式标记
    pv = st.get("policy_version")
    assert pv is None or (isinstance(pv, dict) and ("profile_id" in pv or "name" in pv))
    pa = st.get("pending_approval")
    assert pa and pa.get("required_role") == "supervisor" and "approve|reject" in str(pa.get("action"))


def test_auto_approve_checkpoint_no_suspend_fields(env):
    """AUTO_APPROVE（128 元）检查点不应带挂起原因（不污染秒退用例）。"""
    cid = _make_case("128.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "COMPLETED"
    st = _ckpt_state(cid)
    # 终态用例挂起字段应为空/缺省；suspend_reason 未被写入（决策节点对非挂起置 None）
    assert not st.get("suspend_reason")


# ---- 检查点 TTL / 终态清理 / 损坏安全失败 ----

def _binary_redis():
    from app.workflow.redis_checkpointer import _binary_redis
    return _binary_redis


def test_suspend_checkpoint_has_ttl(env):
    """挂起案件检查点须带 TTL（超时自动过期，防止无限膨胀）。"""
    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"

    red = _binary_redis()
    from app.workflow.redis_checkpointer import CKPT_PREFIX
    ttl = red.ttl(f"{CKPT_PREFIX}{cid}")
    assert ttl > 0, f"挂起检查点应设置 TTL，实际 ttl={ttl}"
    # 清理，避免残留
    red.delete(f"{CKPT_PREFIX}{cid}", f"langgraph:checkpoint_write:{cid}")


def test_completed_checkpoint_cleaned(env):
    """终态案件跑完后检查点被 runner 清理（不残留 Redis）。"""
    cid = _make_case("128.00")   # 秒退 -> COMPLETED
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "COMPLETED"

    red = _binary_redis()
    from app.workflow.redis_checkpointer import CKPT_PREFIX, WRITE_PREFIX
    assert red.exists(f"{CKPT_PREFIX}{cid}") == 0, "终态案件检查点应被删除"
    assert red.exists(f"{WRITE_PREFIX}{cid}") == 0, "终态案件 pending_writes 应被删除"


def test_corrupt_checkpoint_safe_fails(env):
    """损坏检查点 -> get_tuple 安全失败（返回 None）-> resume 拒绝保持挂起。"""
    import pickle

    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"

    # 篡改检查点内容为非法 pickle
    red = _binary_redis()
    from app.workflow.redis_checkpointer import CKPT_PREFIX
    key = f"{CKPT_PREFIX}{cid}"
    latest = red.hget(key, "latest")
    # 用一个"伪检查点"（结构非法：非二元组）覆盖真实数据
    red.hset(key, latest, pickle.dumps({"not": "a-valid-checkpoint"}))

    # get_tuple 拒绝返回 None
    tup = runner.saver.get_tuple(
        {"configurable": {"thread_id": cid, "checkpoint_id": latest}})
    assert tup is None, "损坏检查点必须安全失败（None），不得反序列化非结构数据"

    # resume 应拒绝恢复，保持挂起
    outcome2 = runner.resume(cid, "APPROVED", "情况属实，批准退款")
    assert outcome2 == "SUSPENDED"
    assert _case_state(cid)[0] == CASE_STATUS_SUSPENDED
    # 清理篡改数据
    red.delete(key, f"langgraph:checkpoint_write:{cid}")
