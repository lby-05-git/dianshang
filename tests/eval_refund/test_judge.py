# -*- coding: utf-8 -*-
"""M6 确定性 Judge + 可选 LLM Judge 契约测试（..J07）。"""
import pytest

from eval_refund import judge
from eval_refund.judge import (
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    STATUS_COMPLETED,
    STATUS_SUSPENDED,
    deterministic_judge,
    judge_llm,
    metric_names,
)
from eval_refund.schema import CandidateOutput, REFUND_NONE


def _out(status=STATUS_COMPLETED, decision=DECISION_APPROVED,
         refund="SUCCESS", order="MATCH", product="MATCH", price="MATCH",
         node="finalize", case_id="G01") -> CandidateOutput:
    return CandidateOutput(
        case_id=case_id, current_status=status, final_decision=decision,
        refund_status=refund, order_verification=order,
        product_consistency=product, price_consistency=price,
        current_graph_node=node,
    )


def _g01_golden() -> dict:
    return {
        "id": "G01",
        "expected": {
            "current_status": STATUS_COMPLETED, "final_decision": DECISION_APPROVED,
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "required_nodes": ["finalize"],
        "forbidden_nodes": [],
    }


def test_pass_on_matching_case():
    res = deterministic_judge(_out(), _g01_golden())
    assert res.verdict == "pass"
    assert res.score == 1.0
    assert res.failure_reason == ""


def test_fail_on_wrong_status():
    res = deterministic_judge(
        _out(status=STATUS_SUSPENDED, decision=DECISION_PENDING, refund=REFUND_NONE),
        _g01_golden())
    assert res.verdict == "fail"
    assert "status" in res.failure_reason
    assert res.score < 1.0


def test_fail_on_wrong_decision():
    res = deterministic_judge(
        _out(decision=DECISION_REJECTED, refund=REFUND_NONE), _g01_golden())
    assert res.verdict == "fail"
    assert "decision" in res.failure_reason


def test_redline_refund_side_effect_not_erased_by_passing_dimensions():
    """核心红线：未批准却退款 -> 即使其余维度全过，整体仍是 fail（不平均抵消）。"""
    # 决策 REJECTED 但 refund_status 却是 SUCCESS —— 资金副作用，绝不能 pass
    out = _out(decision=DECISION_REJECTED, refund="SUCCESS")
    golden = _g01_golden()
    golden["expected"]["final_decision"] = DECISION_REJECTED
    golden["expected"]["refund_status"] = REFUND_NONE
    res = deterministic_judge(out, golden)
    assert res.verdict == "fail"
    assert "资金副作用" in res.failure_reason or "退款副作用" in res.failure_reason


def test_verification_allowed_range():
    """G07 类：price MISMATCH 在 allow 集合内 -> verification 维通过。"""
    out = _out(order="MATCH", product="MATCH", price="MISMATCH",
               status=STATUS_SUSPENDED, decision=DECISION_PENDING,
               refund=REFUND_NONE, node="human_review")
    golden = {
        "id": "G07",
        "expected": {
            "current_status": STATUS_SUSPENDED, "final_decision": DECISION_PENDING,
            "refund_status": REFUND_NONE,
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MISMATCH",
        },
        "allow": {"price_consistency": ["MISMATCH"]},
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
    }
    res = deterministic_judge(out, golden)
    dims = {d["name"]: d["ok"] for d in res.dimensions}
    assert dims["verification_price"] is True
    assert res.verdict == "pass"


def test_verification_outside_allow_fails():
    """price 期望 MISMATCH 但得到 MATCH -> 核验维失败。"""
    out = _out(order="MATCH", product="MATCH", price="MATCH",
               status=STATUS_SUSPENDED, decision=DECISION_PENDING,
               refund=REFUND_NONE, node="human_review")
    golden = {
        "id": "G07",
        "expected": {
            "current_status": STATUS_SUSPENDED, "final_decision": DECISION_PENDING,
            "refund_status": REFUND_NONE, "price_consistency": "MISMATCH",
        },
        "allow": {"price_consistency": ["MISMATCH"]},
        "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
    }
    res = deterministic_judge(out, golden)
    assert res.verdict == "fail"
    assert "verification_price" in res.failure_reason


def test_forbidden_node_fails():
    """秒退却停在 human_review -> 禁止节点违例 -> fail。"""
    out = _out(node="human_review")
    golden = _g01_golden()
    golden["forbidden_nodes"] = ["human_review"]
    res = deterministic_judge(out, golden)
    assert res.verdict == "fail"
    assert "nodes" in res.failure_reason


def test_judge_version_present():
    assert judge.JUDGE_VERSION == "1.0"


def test_result_has_full_f15_j03_fields():
    res = deterministic_judge(_out(), _g01_golden())
    d = res.as_dict()
    assert set(d) == {"case_id", "verdict", "score", "dimensions", "evidence",
                      "judge_version", "failure_reason", "llm", "degraded_reason"}


def test_llm_judge_unconfigured_falls_back():
    """：LLM 未配置 -> fallback，绝不冒充 LLM 成功。"""
    llm = judge_llm(_out(), _g01_golden(), llm_client=None)
    assert llm["judged"] is False
    assert llm["fallback"] is True
    assert llm["dimensions"]["sentiment_explanation"] is None


def test_llm_judge_client_called_and_parsed():
    class FakeResp:
        def complete(self, prompt):
            return '{"sentiment_explanation": 4, "summary_faithful": 4.5, ' \
                   '"completeness": 5, "expression_quality": 4, "summary": "ok"}'

    llm = judge_llm(_out(), _g01_golden(), llm_client=FakeResp())
    assert llm["judged"] is True
    assert llm["fallback"] is False
    assert llm["dimensions"]["completeness"] == 5.0
    assert llm["dimensions"]["summary_faithful"] == 4.5


def test_llm_judge_exception_falls_back():
    class BadResp:
        def complete(self, prompt):
            raise RuntimeError("judge down")

    llm = judge_llm(_out(), _g01_golden(), llm_client=BadResp())
    assert llm["judged"] is False
    assert llm["fallback"] is True


def test_metrics_registry_reserved_only():
    """/metrics：security/intent/voice/coaching 只预订不假装实现。"""
    names = metric_names()
    assert "refund.status" in names
    assert "verification.order" in names
    # 预留组未注册 compute -> 不应假装注册为可用
    assert judge.METRICS["security.redline"]["registered"] is False
    from eval_refund.judge import METRICS
    assert METRICS["voice.asr"]["compute"] is None