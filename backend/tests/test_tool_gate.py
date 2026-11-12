# -*- coding: utf-8 -*-
"""退款工具前置策略校验（tool gate）单测（基线 V1.0）。

覆盖：
- 状态不符（SUSPENDED/COMPLETED）-> 拒绝；
- 决策不符（PENDING/REJECTED）-> 拒绝；
- 核验不符（任一 MISMATCH/UNKNOWN/None）-> 拒绝；
- 幂等（已退 status / 已占用退款流水号 / 已有回执）-> 拒绝；
- 全通过 -> 允许，且不落 BLOCK 审计；
- 拒绝会落 security_decisions（source=refund_tool, tool_name=execute_refund）。
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
    REFUND_STATUS_NONE,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    SecurityDecision,
    User,
)
from app.services import tool_gate as tg
from tests.conftest import SessionTest


def _fresh_case(**kw) -> RefundCase:
    """构造一个未落库的 RefundCase 对象（tool gate 是纯读校验，不入簿即可）。"""
    vals = dict(
        order_id=f"ORD-GATE-{uuid.uuid4().hex[:12]}",
        requested_amount=Decimal("99.00"),
        submitter_id="u-cs1",
        current_status=CASE_STATUS_RUNNING,
        final_decision=DECISION_APPROVED,
        refund_status=REFUND_STATUS_NONE,
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="MATCH",
    )
    vals.update(kw)
    return RefundCase(**vals)


@pytest.fixture()
def s():
    return SessionTest()


# ---------- 全通过 ----------

def test_all_ok_allows(s):
    """RUNNING + APPROVED + 三项 MATCH + 未退 -> 允许。"""
    c = _fresh_case()
    dec = tg.check_refund_gate(s, case=c, persist=True)
    assert dec.allow is True
    assert dec.gate_result == "PASS"
    assert dec.reason == []


# ---------- 状态不符 ----------

@pytest.mark.parametrize("status", [CASE_STATUS_SUSPENDED, CASE_STATUS_COMPLETED])
def test_status_not_refundable_rejects(s, status):
    c = _fresh_case(current_status=status)
    dec = tg.check_refund_gate(s, case=c, persist=False)
    assert dec.allow is False
    assert any("状态" in r for r in dec.reason)


# ---------- 决策不符 ----------

@pytest.mark.parametrize("decision", [DECISION_PENDING, DECISION_REJECTED])
def test_decision_not_approved_rejects(s, decision):
    c = _fresh_case(final_decision=decision)
    dec = tg.check_refund_gate(s, case=c, persist=False)
    assert dec.allow is False
    assert any("决策" in r for r in dec.reason)


# ---------- 核验不符 ----------

@pytest.mark.parametrize("v", ["MISMATCH", "UNKNOWN", None])
def test_verify_any_not_match_rejects(s, v):
    c = _fresh_case(order_verification=v)
    dec = tg.check_refund_gate(s, case=c, persist=False)
    assert dec.allow is False
    assert any("核验" in r for r in dec.reason)


def test_verify_all_bad_lists_fields(s):
    c = _fresh_case(order_verification="MISMATCH", product_verification="UNKNOWN",
                    price_verification=None)
    dec = tg.check_refund_gate(s, case=c, persist=False)
    assert dec.allow is False
    assert any("order_verification" in r for r in dec.reason)
    assert any("product_verification" in r for r in dec.reason)
    assert any("price_verification" in r for r in dec.reason)


# ---------- 幂等 ----------

@pytest.mark.parametrize("refund_status", [REFUND_STATUS_SUCCESS, REFUND_STATUS_PENDING])
def test_already_refunded_rejects(s, refund_status):
    c = _fresh_case(refund_status=refund_status)
    dec = tg.check_refund_gate(s, case=c, persist=False)
    assert dec.allow is False
    assert any("幂等" in r or "已退" in r for r in dec.reason)


def test_refund_inflight_key_blocks(s):
    """退款流水号已被占用（Redis refund:{case_id} 存在）-> 幂等拒绝。"""
    c = _fresh_case()
    dec = tg.check_refund_gate(s, case=c, persist=False,
                               refund_id_key="RF-ALREADY-EXISTS")
    assert dec.allow is False
    assert any("退款流水号" in r for r in dec.reason)


def test_refund_receipt_blocks(s):
    """refund_transactions 已有回执 -> 幂等拒绝。"""
    c = _fresh_case()
    dec = tg.check_refund_gate(s, case=c, persist=False, has_refund_receipt=True)
    assert dec.allow is False


# ---------- 落库审计 ----------

def _persisted_case(s, **kw) -> RefundCase:
    """构造并落库一个 RefundCase（FK actions: submitter_id 必须指向真实 user）。"""
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = _fresh_case(submitter_id=u.id, **kw)   # 覆盖 submitter_id（_fresh_case 默认 u-cs1）
    s.add(c)
    s.flush()
    s.commit()
    return c


def test_block_persists_security_decision():
    """拒绝时落 security_decisions（source=refund_tool, tool_name=execute_refund）。"""
    s = SessionTest()
    c = _persisted_case(s, current_status=CASE_STATUS_SUSPENDED)   # 状态不符
    cid = c.id

    dec = tg.check_refund_gate(s, case=c, persist=True)
    assert dec.allow is False

    row = (s.query(SecurityDecision)
           .filter(SecurityDecision.case_id == cid)
           .order_by(SecurityDecision.created_at.desc())
           .first())
    assert row is not None
    assert row.source == "refund_tool"
    assert row.tool_name == "execute_refund"
    assert row.gate_result == "BLOCK"
    assert row.masked_input          # 存了原因/文本
    s.close()


def test_pass_persists_nothing():
    """全通过且 persist=True -> 不落 BLOCK 审计（无安全审计行）。"""
    s = SessionTest()
    c = _persisted_case(s)
    cid = c.id
    tg.check_refund_gate(s, case=c, persist=True)
    n = (s.query(SecurityDecision)
         .filter(SecurityDecision.case_id == cid).count())
    s.close()
    assert n == 0