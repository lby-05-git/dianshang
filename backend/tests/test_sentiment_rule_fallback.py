# -*- coding: utf-8 -*-
"""LLM 不可用时的本地舆情规则回退。"""

from app.core.config import settings
from app.infrastructure.llm import SentimentProvider, rule_based_sentiment


def test_rule_fallback_keeps_normal_refund_low_risk():
    result = rule_based_sentiment("申请退款，不想要了，商品保持完好")
    assert result.sentiment_level == "LOW"
    assert "local_rule_fallback" in result.keywords


def test_rule_fallback_recognizes_dissatisfaction_as_medium():
    result = rule_based_sentiment("商品体验太差了，我要给差评")
    assert result.sentiment_level == "MEDIUM"


def test_rule_fallback_keeps_public_opinion_red_flags_high():
    result = rule_based_sentiment("不给退款我就联系媒体曝光并且投诉到12315")
    assert result.sentiment_level == "HIGH"
    assert "媒体" in result.keywords
    assert "12315" in result.keywords


def test_provider_uses_rule_fallback_only_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "SENTIMENT_RULE_FALLBACK_ENABLED", True)
    provider = SentimentProvider()
    monkeypatch.setattr(provider, "_try_chain", lambda *_args: None)
    result = provider.analyze("不想要了，申请正常退款")
    assert result.sentiment_level == "LOW"
    assert result.keywords == ["local_rule_fallback"]
