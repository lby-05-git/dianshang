# -*- coding: utf-8 -*-
"""两级意图路由单测（基线 V1.0）。

覆盖：
- 规则层命中 6 意图（REFUND/QUERY/EVIDENCE/APPEAL/HUMAN/COMPLAINT_ESCALATE）；
- LLM 层 fallback：规则未命中 + LLM 返回合法意图；
- 截断 JSON 一次修复；
- Fail-Safe：LLM 不可用 / 输出不合法 -> 转 HUMAN，绝不返回 UNKNOWN；
- 只落 sha256，不落明文。
"""
import hashlib
import uuid
from decimal import Decimal

import pytest

from app.domain.models import (
    DECISION_PENDING,
    CASE_STATUS_RUNNING,
    INTENT_APPEAL,
    INTENT_COMPLAINT_ESCALATE,
    INTENT_EVIDENCE,
    INTENT_HUMAN,
    INTENT_QUERY,
    INTENT_REFUND,
    IntentResult,
    RefundCase,
    User,
)
from app.services.intent_router import (
    INTENT_UNKNOWN,
    route_intent,
    sha256_hex,
)
from tests.conftest import SessionTest


# ---------- 构造 RetundCase ----------

def _make_case() -> str:
    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x", role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=f"ORD-INT-{uuid.uuid4().hex[:12]}",
                   requested_amount=Decimal("99.00"),
                   submitter_id=u.id,
                   current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING,
                   refund_reason="")
    s.add(c)
    s.flush()
    cid = c.id
    s.commit()
    s.close()
    return cid


# ---------- 规则层 6 意图 ----------

@pytest.mark.parametrize("text,expected", [
    ("我要申请退款，钱退到我账户", INTENT_REFUND),
    ("快递到哪了？什么时候送到", INTENT_QUERY),
    ("把开箱视频和发票截图发你", INTENT_EVIDENCE),
    ("我不同意这个结果，要申诉", INTENT_APPEAL),
    ("请转人工客服帮我处理", INTENT_HUMAN),
    ("我要投诉你们，举报到12315", INTENT_COMPLAINT_ESCALATE),
])
def test_rule_layer_six_intents(text, expected):
    """规则层命中全部 6 意图。"""
    res = route_intent(None, case_id="x", text=text, llm_route=None, persist=False)
    assert res.final_intent == expected
    assert res.level_1_rule == expected
    assert res.level_2_llm is None


def test_rule_layer_priority_escalation_over_refund():
    """投诉/人工优先级高于退款：同时含退款词 + 投诉词 -> 投诉升级。"""
    res = route_intent(None, case_id="x",
                       text="我要退款但也要投诉你们，还要转人工",
                       llm_route=None, persist=False)
    assert res.final_intent in (INTENT_COMPLAINT_ESCALATE, INTENT_HUMAN)
    assert res.level_1_rule is not None


def test_rule_nomal_text_no_hit_uses_llm():
    """普通客诉文本规则未命中 -> 交给 LLM 层。"""
    def fake_llm(prompt):
        return {"content": '{"intent": "REFUND", "confidence": 0.8}'}

    res = route_intent(None, case_id="x", text="东西坏了很生气",
                       llm_route=fake_llm, persist=False)
    assert res.final_intent == INTENT_REFUND
    assert res.retries_used == 1          # LLM 首轮就命中


def test_llm_truncated_json_repaired_once():
    """LLM 输出截断 JSON：一次修复后命中。"""
    def fake_llm(prompt):
        # 截断：缺闭合 }
        return {"content": '{"intent": "REFUND", "confidence": 0.7'}

    # 文本"东西坏了求处理"规则不命中，走 LLM 层
    res = route_intent(None, case_id="x", text="东西坏了求处理",
                       llm_route=fake_llm, persist=False)
    assert res.final_intent == INTENT_REFUND
    assert res.retries_used == 1


def test_fail_safe_llm_unavailable_turns_human():
    """LLM 不可用 -> Fail-Safe 转 HUMAN，绝不返回 UNKNOWN。"""
    res = route_intent(None, case_id="x", text="某段未命中规则的正文",
                       llm_route=None, persist=False)
    assert res.final_intent == INTENT_HUMAN
    assert res.level_2_llm is None
    assert res.risk == "MEDIUM"


def test_fail_safe_llm_invalid_output_turns_human():
    """LLM 输出不合法且修复也无效 -> HUMAN。"""
    def bad_llm(prompt):
        return {"content": "完全不是JSON"}

    res = route_intent(None, case_id="x", text="随便一段文本",
                       llm_route=bad_llm, persist=False)
    assert res.final_intent == INTENT_HUMAN
    assert res.retries_used == 2          # 两次尝试用完


# ---------- 落库只落 sha256 ----------

def test_persist_writes_sha256_not_plaintext():
    """落库 intent_results：只存 sha256，原文不落库。"""
    cid = _make_case()
    text = "我要申请退款，钱退到账户 13812345678"
    s = SessionTest()
    route_intent(s, case_id=cid, text=text, llm_route=None, persist=True)
    row = (s.query(IntentResult)
           .filter(IntentResult.case_id == cid)
           .order_by(IntentResult.created_at.desc())
           .first())
    s.close()
    assert row is not None
    assert row.raw_sha256 == sha256_hex(text)
    assert row.final_intent == INTENT_REFUND
    assert "13812345678" not in str(row.raw_sha256)   # 明文绝不在原始哈希之外出现
    # 校验不落明文：ORM 里没有可容纳原文的列
    assert not hasattr(row, "raw_text")