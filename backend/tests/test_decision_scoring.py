from app.services.decision_scoring import build_decision_scores


def test_scores_are_derived_from_real_case_facts():
    result = build_decision_scores(
        ocr_summary={"confidence": 0.8},
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="UNKNOWN",
        fraud_score=67,
        sentiment_level="MEDIUM",
    )

    assert result["version"] == "decision-score-v1"
    by_key = {item["key"]: item for item in result["dimensions"]}
    assert by_key["evidence_confidence"]["score"] == 80
    assert by_key["fraud_risk"]["score"] == 67
    assert by_key["sentiment_risk"]["score"] == 60


def test_incomplete_steps_are_pending_instead_of_fake_zero():
    result = build_decision_scores(
        ocr_summary=None,
        order_verification=None,
        product_verification=None,
        price_verification=None,
        fraud_score=None,
        sentiment_level=None,
    )

    assert all(item["score"] is None for item in result["dimensions"])
    assert all(item["level"] == "PENDING" for item in result["dimensions"])


def test_out_of_range_fraud_score_is_clamped():
    result = build_decision_scores(
        ocr_summary={"confidence": 1},
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="MATCH",
        fraud_score=130,
        sentiment_level="HIGH",
    )

    by_key = {item["key"]: item for item in result["dimensions"]}
    assert by_key["fraud_risk"]["score"] == 100


def test_scores_expose_case_specific_reasons_for_all_dimensions():
    result = build_decision_scores(
        ocr_summary={"confidence": 0.98},
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="MATCH",
        fraud_score=0,
        sentiment_level="HIGH",
        order_reasons=["订单归属一致"],
        product_reasons=["用户商品诉求命中订单商品"],
        price_reasons=["申请金额5799在可退5799内"],
        fraud_reasons=[],
        sentiment_keywords=["商品质量问题", "摄像头损坏"],
    )

    by_key = {item["key"]: item for item in result["dimensions"]}
    assert any("订单归属一致" in reason for reason in by_key["evidence_confidence"]["reasons"])
    assert "未命中已配置的欺诈风险规则" in by_key["fraud_risk"]["reasons"]
    assert any("摄像头损坏" in reason for reason in by_key["sentiment_risk"]["reasons"])


def test_insufficient_evidence_caps_confidence_instead_of_showing_high_confidence():
    result = build_decision_scores(
        ocr_summary={
            "confidence": 0.98,
            "consistency": {"verdict": "INSUFFICIENT", "reason": "缺少订单凭证"},
        },
        order_verification="MATCH",
        product_verification="MATCH",
        price_verification="MATCH",
        fraud_score=0,
        sentiment_level="LOW",
    )

    evidence = next(item for item in result["dimensions"] if item["key"] == "evidence_confidence")
    assert evidence["score"] == 69
    assert any("封顶" in reason for reason in evidence["reasons"])
