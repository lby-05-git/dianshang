# -*- coding: utf-8 -*-
"""LangGraph 图状态（docs/08 第5阶段建议的工作流状态字段）。

金额在图中一律用整数分（amount_cents），杜绝二进制浮点；
Checkpointer 序列化 JSON 友好（无 Decimal/datetime 对象）。
"""
from typing import TypedDict


class GraphState(TypedDict, total=False):
    case_id: str
    trace_id: str
    amount_cents: int
    evidence_path: str | None
    refund_reason: str | None

    # OCR
    ocr_text: str | None
    ocr_fields: dict
    ocr_confidence: float
    ocr_status: str            # SUCCESS / UNRELIABLE / FAILED

    # 多模态凭证（OCR 精确字段 + Qwen2.5-VL 视觉事实 + 图文一致性）
    evidence_status: str       # SUCCESS / DEGRADED / UNRELIABLE / FAILED / UNSAFE
    visual_evidence: dict
    evidence_risk_score: int
    evidence_risk_rules: list[str]

    # Fraud（规则组合评分，裁决 D4）
    fraud_score: int
    fraud_rules: list[str]

    # Sentiment（LLM，裁决 D2）
    sentiment_level: str       # LOW / MEDIUM / HIGH
    sentiment_keywords: list[str]

    # 订单/商品/价格核验（；verification 节点写）
    order_verification: str    # MATCH / MISMATCH / UNKNOWN
    order_verify_reasons: list[str]
    product_verification: str  # MATCH / MISMATCH / UNKNOWN
    product_verify_reasons: list[str]
    price_verification: str    # MATCH / MISMATCH / UNKNOWN
    price_verify_reasons: list[str]

    # 决策（docs/06 ）
    decision: str              # HUMAN_REVIEW / AUTO_APPROVE / APPROVED / REJECTED / SYSTEM_FAILURE
    review_reason: str | None

    # ：挂起前检查点快照追加字段
    # 状态机不依赖以下字段，仅写入 Checkpointer 供恢复/报告/审计还原"当时用了什么、为何挂起、等谁审批"
    policy_version: dict | None   # {profile_id, name} 决策时生效策略档标识
    suspend_reason: str | None    # 独立挂起原因（amount_decision 产出；与 review_reason 区分）
    pending_approval: dict | None # 待审批信息 {required_role, action, note}

    errors: list[str]
