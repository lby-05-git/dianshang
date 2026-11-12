# -*- coding: utf-8 -*-
"""案件三维评分。

评分只由已经落库的业务事实派生，不引入随机值，也不把未完成的 Agent
步骤伪装成 0 分。这样同一案件重复查询会得到稳定、可解释的结果。
"""
from __future__ import annotations

from typing import Any


_VERIFICATION_SCORE = {"MATCH": 100, "UNKNOWN": 40, "MISMATCH": 0}
_SENTIMENT_SCORE = {"LOW": 20, "MEDIUM": 60, "HIGH": 90}
_FRAUD_RULE_LABELS = {
    "image_prompt_injection": "附件中检测到提示词注入",
    "evidence_field_conflict": "OCR 与视觉字段存在冲突",
    "claim_contradicted": "附件事实与用户描述矛盾",
    "evidence_irrelevant": "附件与本次申请无关",
    "order_mismatch": "订单真实性核验不匹配",
    "product_mismatch": "购买商品一致性核验不匹配",
    "price_mismatch": "申请金额核验不匹配",
    "evidence_reuse": "同一凭证在其他案件中重复使用",
    "high_frequency": "同一订单24小时内高频申请",
    "high_refund_rate": "同一订单存在多次历史申请",
    "threshold_carding": "申请金额疑似卡自动退款阈值",
}


def _clamp_score(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return round(max(0.0, min(100.0, float(value))))
    except (TypeError, ValueError):
        return None


def _risk_level(score: int | None) -> str:
    if score is None:
        return "PENDING"
    if score >= 60:
        return "HIGH"
    if score <= 20:
        return "LOW"
    return "MEDIUM"


def _fraud_rule_label(rule: object) -> str:
    value = str(rule)
    return _FRAUD_RULE_LABELS.get(value, value)


def _evidence_score(
    ocr_summary: dict | None,
    order_verification: str | None,
    product_verification: str | None,
    price_verification: str | None,
    verification_reasons: dict[str, list[str]] | None = None,
) -> tuple[int | None, str, list[str]]:
    statuses = (order_verification, product_verification, price_verification)
    confidence = (ocr_summary or {}).get("confidence")
    try:
        ocr_score = _clamp_score(float(confidence) * 100) if confidence is not None else None
    except (TypeError, ValueError):
        ocr_score = None

    # 四个证据信号必须齐全后才给分，避免流程进行中出现误导性的低分。
    if ocr_score is None or any(status not in _VERIFICATION_SCORE for status in statuses):
        return None, "等待 OCR 与订单、商品、价格三项核验全部完成", [
            "至少一个评分输入尚未产生，系统不会用0分冒充未完成结果",
        ]

    labels = ("订单真实性", "商品一致性", "价格一致性")
    keys = ("order", "product", "price")
    verdict_text = {"MATCH": "匹配", "UNKNOWN": "未知", "MISMATCH": "不匹配"}
    reasons = [f"多模态综合置信度 {ocr_score}%，计 {ocr_score} 分"]
    reason_map = verification_reasons or {}
    for label, key, status in zip(labels, keys, statuses):
        points = _VERIFICATION_SCORE[status]
        detail = "；".join(str(v) for v in reason_map.get(key, []) if v)
        line = f"{label}：{verdict_text[status]}，计 {points} 分"
        reasons.append(f"{line}（{detail}）" if detail else line)

    score = round((ocr_score + sum(_VERIFICATION_SCORE[status] for status in statuses)) / 4)
    consistency = (ocr_summary or {}).get("consistency") or {}
    consistency_verdict = consistency.get("verdict")
    consistency_reason = consistency.get("reason")
    caps = {"INSUFFICIENT": 69, "CONFLICT": 30, "UNRELATED": 30, "UNSAFE": 0}
    if consistency_verdict in caps and score > caps[consistency_verdict]:
        score = caps[consistency_verdict]
        reasons.append(f"附件综合判定为 {consistency_verdict}，可信度封顶 {score} 分")
    if consistency_reason:
        reasons.append(f"附件综合结论：{consistency_reason}")
    reasons.append(f"最终证据可信度：{score} 分")
    return score, "综合置信度与订单、商品、价格核验等权计算，并受附件完整性约束", reasons


def build_decision_scores(
    *,
    ocr_summary: dict | None,
    order_verification: str | None,
    product_verification: str | None,
    price_verification: str | None,
    fraud_score: int | None,
    sentiment_level: str | None,
    order_reasons: list[str] | None = None,
    product_reasons: list[str] | None = None,
    price_reasons: list[str] | None = None,
    fraud_reasons: list[str] | None = None,
    sentiment_keywords: list[str] | None = None,
) -> dict:
    """返回适合 API 与前端直接消费的三维评分结构。"""
    evidence_score, evidence_explanation, evidence_reasons = _evidence_score(
        ocr_summary, order_verification, product_verification, price_verification,
        {"order": order_reasons or [], "product": product_reasons or [], "price": price_reasons or []},
    )
    fraud = _clamp_score(fraud_score)
    sentiment = _SENTIMENT_SCORE.get((sentiment_level or "").upper())

    return {
        "version": "decision-score-v1",
        "dimensions": [
            {
                "key": "evidence_confidence",
                "label": "证据可信度",
                "score": evidence_score,
                "level": _risk_level(100 - evidence_score) if evidence_score is not None else "PENDING",
                "direction": "HIGHER_BETTER",
                "explanation": evidence_explanation,
                "reasons": evidence_reasons,
            },
            {
                "key": "fraud_risk",
                "label": "欺诈风险",
                "score": fraud,
                "level": _risk_level(fraud),
                "direction": "HIGHER_WORSE",
                "explanation": "直接使用欺诈风控 Agent 的 0–100 分结果" if fraud is not None else "等待欺诈风控 Agent 完成",
                "reasons": ([f"欺诈风控 Agent 输出 {fraud} 分"] + (
                    [f"命中规则：{'、'.join(_fraud_rule_label(v) for v in (fraud_reasons or []))}"]
                    if fraud_reasons else ["未命中已配置的欺诈风险规则"]
                )) if fraud is not None else ["欺诈风控 Agent 尚未完成"],
            },
            {
                "key": "sentiment_risk",
                "label": "舆情风险",
                "score": sentiment,
                "level": (sentiment_level or "PENDING").upper(),
                "direction": "HIGHER_WORSE",
                "explanation": "由舆情等级 LOW/MEDIUM/HIGH 映射为 20/60/90，便于同屏比较"
                if sentiment is not None else "等待舆情分析 Agent 完成",
                "reasons": ([
                    f"舆情分析等级 {(sentiment_level or '').upper()}，映射为 {sentiment} 分",
                    f"识别关键词：{'、'.join(str(v) for v in (sentiment_keywords or []))}"
                    if sentiment_keywords else "未返回显著舆情关键词",
                ]) if sentiment is not None else ["舆情分析 Agent 尚未完成"],
            },
        ],
    }
