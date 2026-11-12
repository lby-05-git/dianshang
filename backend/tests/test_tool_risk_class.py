# -*- coding: utf-8 -*-
"""V3.0 V3-3 工具风险分级 + 退款类「策略+HITL+幂等+审计」四同时。

覆盖：
- 四级风险类静态默认：execute_refund=fund_effect / notification_send=external_notify /
  external_query=readonly / document_generate=internal_write；未知工具最低只读；
- DB tool_definitions 覆盖（enabled=False 停用 -> 策略维拒绝）；
- fund_effect 四同时：策略/人审/幂等/审计 任一缺失 -> BLOCK（不会自动退款）；
- persist=True 时四同时判定落 security_decisions + TOOL_GATE 事件（审计留痕）；
- 非资金类工具不要求人审/幂等（只校验 策略+审计）；
- 回归：默认健康业务下合规 case 放行（蓝方护栏非砖墙）。

共享测试库踩坑：整库复用，相对/键断言，不数绝对行数。
"""
import uuid
from decimal import Decimal

import pytest

from app.domain.models import (
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    REFUND_STATUS_NONE,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    SecurityDecision,
    ToolDefinition,
    new_uuid,
)
from app.services import security_gateway as sg
from tests.conftest import SessionTest


@pytest.fixture(autouse=True)
def _clean_tool_defs():
    """整库复用：清掉本模块可能落库的 ToolDefinition 覆盖行。

    前次运行中断会残留 execute_refund 停用行 -> 毒化后续所有资金类用例
    （strategy 维提前 FAIL）。每测前清空本模块用到的 tool_name，保证隔离。
    """
    s = SessionTest()
    try:
        s.query(ToolDefinition).filter(
            ToolDefinition.tool_name.in_(("execute_refund", "order_lookup"))
        ).delete()
        s.commit()
    finally:
        s.close()
    yield


def _case(**kw) -> RefundCase:
    vals = dict(
        id=f"tk-{uuid.uuid4().hex[:10]}",
        order_id="ORD-RISK-1",
        requested_amount=Decimal("99.00"),
        submitter_id="u-risk",
        current_status=CASE_STATUS_RUNNING,
        final_decision=DECISION_APPROVED,
        refund_status=REFUND_STATUS_NONE,
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="MATCH",
    )
    vals.update(kw)
    return RefundCase(**vals)


# ---------- 静态默认分级 ----------

def test_static_risk_class_mapping():
    assert sg.classify_tool_risk(None, "execute_refund")["risk_class"] == "fund_effect"
    assert sg.classify_tool_risk(None, "notification_send")["risk_class"] == "external_notify"
    assert sg.classify_tool_risk(None, "external_query")["risk_class"] == "readonly"
    assert sg.classify_tool_risk(None, "document_generate")["risk_class"] == "internal_write"
    assert sg.classify_tool_risk(None, "order_lookup")["risk_class"] == "readonly"


def test_unknown_tool_defaults_readonly():
    cfg = sg.classify_tool_risk(None, "some_new_tool")
    assert cfg["risk_class"] == "readonly"
    assert cfg["enabled"] is True


# ---------- fund_effect 四同时 ----------

def test_refund_four_simultaneously_all_pass():
    s = SessionTest()
    try:
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund", case=_case(),
            hitl_approved=True, persist=False, audit_committed=True)
        assert res.allow is True
        assert res.risk_class == "fund_effect"
        assert res.missing == []
    finally:
        s.close()


def test_refund_missing_hitl_blocks():
    s = SessionTest()
    try:
        # 决策 PENDING（无人工审批冲突）+ hitl_approved 缺省 -> 人审维 FAIL
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund",
            case=_case(final_decision=DECISION_PENDING),
            persist=False, audit_committed=True)
        assert res.allow is False
        dims = [d for d, _ in res.missing]
        assert "hitl" in dims
        assert res.missing and any("人审" in r for _, r in res.missing)
    finally:
        s.close()


def test_refund_missing_idempotency_blocks():
    """重复退款（已退 SUCCESS）-> 幂等维 FAIL -> BLOCK（绝不重复退）。"""
    s = SessionTest()
    try:
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund",
            case=_case(refund_status=REFUND_STATUS_SUCCESS),
            hitl_approved=True, persist=False, audit_committed=True)
        assert res.allow is False
        dims = [d for d, _ in res.missing]
        assert "idempotency" in dims
    finally:
        s.close()


def test_refund_missing_audit_blocks():
    """资金类放行必须留审计：persist=False 且未承诺 audit_committed -> 审计维 FAIL。"""
    s = SessionTest()
    try:
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund", case=_case(),
            hitl_approved=True, persist=False, audit_committed=False)
        assert res.allow is False
        dims = [d for d, _ in res.missing]
        assert "audit" in dims
    finally:
        s.close()


def test_refund_disabled_tool_strategy_blocks():
    """DB tool_definitions 停用 execute_refund -> 策略维 FAIL（Fail-Closed）。"""
    s = SessionTest()
    try:
        s.add(ToolDefinition(
            id=new_uuid(), tool_name="execute_refund", risk_class="fund_effect",
            description="test disable", enabled=False))
        s.commit()
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund", case=_case(),
            hitl_approved=True, persist=False, audit_committed=True)
        assert res.allow is False
        dims = [d for d, _ in res.missing]
        assert "strategy" in dims
    finally:
        # autouse fixture 已兜底；这里再保险一次，防中断残留
        s.query(ToolDefinition).filter_by(tool_name="execute_refund").delete()
        s.commit()
        s.close()


def _persisted_case(s, **kw) -> RefundCase:
    """构造并落库一个 RefundCase（persist=True 时 FK 需要真实 user）。"""
    from app.domain.models import User
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x",
                 role="customer_service")
        s.add(u)
        s.flush()
    c = _case(submitter_id=u.id, **kw)
    s.add(c)
    s.flush()
    s.commit()
    return c


# ---------- 非资金类工具只要求策略+审计 ----------

def test_readonly_tool_does_not_require_hitl():
    s = SessionTest()
    try:
        c = _persisted_case(s, final_decision=DECISION_PENDING)
        res = sg.check_tool_risk_gate(
            s, tool_name="order_lookup", case=c,
            persist=True, audit_committed=True)
        assert res.allow is True          # readonly 不要求人审/幂等
        assert res.risk_class == "readonly"
    finally:
        s.close()


# ---------- persist=True 审计落库 ----------

def test_block_persists_security_decision_and_event():
    s = SessionTest()
    try:
        c = _persisted_case(s, refund_status=REFUND_STATUS_SUCCESS)   # 幂等维破坏
        cid = c.id
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund", case=c,
            hitl_approved=True, persist=True)
        assert res.allow is False and res.persisted is True
        row = (s.query(SecurityDecision)
               .filter_by(case_id=cid, tool_name="execute_refund")
               .order_by(SecurityDecision.created_at.desc()).first())
        assert row is not None
        assert row.gate_result == "BLOCK"
        hits = row.rule_hits or {}
        assert hits.get("tool_risk_class") == "fund_effect"
        assert any("幂等" in r for r in hits.get("tool_risk_gate_reasons", []))
        # TOOL_GATE 事件已录
        from app.domain.models import SecurityEvent
        ev = (s.query(SecurityEvent)
              .filter_by(event_type="TOOL_GATE")
              .order_by(SecurityEvent.created_at.desc()).first())
        assert ev is not None and ev.disposition == "OPEN"
    finally:
        s.close()


def test_pass_persists_release_trail():
    """资金类放行也留痕（audit 四同时的"审计在"落地）。"""
    s = SessionTest()
    try:
        c = _persisted_case(s)
        res = sg.check_tool_risk_gate(
            s, tool_name="execute_refund", case=c,
            hitl_approved=True, persist=True)
        assert res.allow is True and res.persisted is True
        row = (s.query(SecurityDecision)
               .filter_by(case_id=c.id, gate_result="PASS")
               .order_by(SecurityDecision.created_at.desc()).first())
        assert row is not None
    finally:
        s.close()