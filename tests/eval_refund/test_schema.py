# -*- coding: utf-8 -*-
"""M4 CandidateOutput schema 1.0 + extensions 命名空间契约测试。"""
import pytest

from eval_refund import schema
from eval_refund.schema import (
    CandidateOutput,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SUSPENDED,
    empty_extensions,
    from_dict,
)


def test_schema_version_is_1_0():
    assert schema.SCHEMA_VERSION == "1.0"


def test_candidate_defaults_are_safe():
    out = CandidateOutput()
    # 未测量的指标必须保持默认与 measured=False（红线：不伪造指标）
    assert out.input_tokens == 0
    assert out.output_tokens == 0
    assert out.cost == 0.0
    assert out.measured is False
    assert out.usage_measured is False
    assert out.latency_ms == 0.0
    assert out.refund_status == schema.REFUND_NONE
    assert out.final_decision == DECISION_PENDING


def test_validate_accepts_valid_states():
    out = CandidateOutput(current_status=STATUS_COMPLETED, final_decision=DECISION_APPROVED)
    out.validate()  # 不抛


@pytest.mark.parametrize("bad_status", ["DONE", "FOO", ""])
def test_validate_rejects_bad_status(bad_status):
    out = CandidateOutput(current_status=bad_status)
    with pytest.raises(ValueError):
        out.validate()


def test_as_dict_structure():
    out = CandidateOutput(
        case_id="G01", trace_id="t-1",
        current_status=STATUS_SUSPENDED, final_decision=DECISION_PENDING,
        refund_status=schema.REFUND_NONE,
        order_verification=schema.VERI_MATCH,
        product_consistency=schema.VERI_MATCH,
        price_consistency=schema.VERI_MATCH,
        latency_ms=12.5,
    )
    d = out.as_dict()
    assert d["schema_version"] == "1.0"
    assert d["target_type"] == "refund_workflow"
    assert d["case_id"] == "G01"
    biz = d["business"]
    assert biz["current_status"] == STATUS_SUSPENDED
    assert biz["final_decision"] == DECISION_PENDING
    assert biz["refund_status"] == schema.REFUND_NONE
    assert biz["order_verification"] == schema.VERI_MATCH
    assert biz["product_consistency"] == schema.VERI_MATCH
    assert biz["price_consistency"] == schema.VERI_MATCH
    # extensions 必须有全部四个命名空间（可为 None = 未实现占位）
    ext = d["extensions"]
    assert set(ext.keys()) == {"security", "intent", "voice", "coaching"}
    assert ext["security"] is None
    assert ext["voice"] is None
    # usage 未测量必须如实 false（成本分账新增 agent/model/model_rates）
    assert d["usage"] == {"input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                          "measured": False, "agent": "business", "model": None,
                          "model_rates": {}}


def test_extensions_do_not_pollute_business():
    """扩展命名空间改动不影响 business 核心字段（向后兼容）。"""
    out = CandidateOutput(current_status=STATUS_RUNNING, extensions_intent={"route": "refund"})
    out.validate()  # intent 数据在 namespaces，不进 business
    assert out.as_dict()["business"]["current_status"] == STATUS_RUNNING
    assert out.as_dict()["extensions"]["intent"] == {"route": "refund"}


def test_from_dict_roundtrip():
    d = {
        "schema_version": "1.0",
        "case_id": "G02",
        "business": {
            "current_status": STATUS_COMPLETED,
            "final_decision": DECISION_REJECTED,
            "refund_status": schema.REFUND_NONE,
            "order_verification": schema.VERI_MATCH,
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001, "measured": True},
        "extensions": {"security": None, "intent": None, "voice": None, "coaching": None},
    }
    out = from_dict(d)
    assert out.current_status == STATUS_COMPLETED
    assert out.final_decision == DECISION_REJECTED
    assert out.input_tokens == 10
    assert out.usage_measured is True


def test_empty_extensions_placeholder():
    assert empty_extensions() == {
        "security": None, "intent": None, "voice": None, "coaching": None,
    }


def test_from_dict_missing_biz_defaults_conservative():
    """缺 business 时不得伪造安全值：状态默认 RUNNING/PENDING，绝不默认已批准。"""
    out = from_dict({"schema_version": "1.0", "case_id": "x"})
    # 缺字段 => 保守默认（RUNNING/PENDING/NONE），绝不是 COMPLETED/APPROVED
    assert out.current_status == "RUNNING"
    assert out.final_decision == "PENDING"
    assert out.refund_status == "NONE"