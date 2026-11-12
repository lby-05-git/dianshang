# -*- coding: utf-8 -*-
"""CandidateOutput schema 1.0（退赔域）与 extensions 命名空间（-T06）。

结构遵循融合版 CandidateOutput 逻辑结构：
  schema_version / target_type / case_id / trace_id /
  business{current_status, final_decision, refund_status, current_graph_node,
           order_verification, product_consistency, price_consistency} /
  usage{input_tokens, output_tokens, cost, measured} / latency / tools / artifacts /
  errors / extensions{security, intent, voice, coaching}

约定：
- schema_version 不可省略；核心字段只向后兼容扩展。
- extensions 为空（None）不代表功能已实现，只表示协议可扩展；扩展能力数据
  进各自命名空间，绝不污染 business 字段。
- 状态/funding 副作用等由确定性 Judge 读取 business 字段判定，不在此伪造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"
TARGET_TYPE = "refund_workflow"

# ---- 状态常量（与 backend/app/domain/models.py 保持一致；评测侧只读展示）----
STATUS_RUNNING = "RUNNING"
STATUS_SUSPENDED = "SUSPENDED"
STATUS_COMPLETED = "COMPLETED"
DECISION_PENDING = "PENDING"
DECISION_APPROVED = "APPROVED"
DECISION_REJECTED = "REJECTED"
DECISION_SYSTEM_FAILURE = "SYSTEM_FAILURE"
REFUND_NONE = "NONE"
REFUND_PENDING = "PENDING"
REFUND_SUCCESS = "SUCCESS"
REFUND_FAILED = "FAILED"
VERI_MATCH = "MATCH"
VERI_MISMATCH = "MISMATCH"
VERI_UNKNOWN = "UNKNOWN"

# extensions 命名空间（固定顺序，前后端/报告都按此序列化）
EXT_NAMESPACES = ("security", "intent", "voice", "coaching")


@dataclass
class CandidateOutput:
    """统一候选输出（schema 1.0）。与评测工程 CandidateOutput 等价但退赔域字段。

    Judge / 成本 / 时延统计只依赖本结构，不感知具体注入方式。
    """

    schema_version: str = SCHEMA_VERSION
    target_type: str = TARGET_TYPE
    case_id: str = ""
    trace_id: str = ""

    # ---- business（三态 + 决策 + 退款状态 + 订单/商品/价格核验）----
    current_status: str = STATUS_RUNNING
    final_decision: str = DECISION_PENDING
    refund_status: str = REFUND_NONE
    current_graph_node: str = "intake"
    order_verification: str = ""
    product_consistency: str = ""
    price_consistency: str = ""

    # ---- 实测指标（禁止虚构；未测量必须保持 measured=False 与默认值）----
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    measured: bool = False

    # ---- 成本分账：usage 归属（agent/model 维度；或 oracle 未测防御为 None）----
    agent: str = "business"          # business=业务执行器；judge=LLM判卷（附加质量维）
    model: str | None = None         # 该 agent 实际使用的模型（无则 None）
    model_rates: dict | None = None  # 计费费率快照（in/out 每百万 tokens ¥，供审计复核）

    # ---- 时延（实测毫秒）----
    latency_ms: float = 0.0

    # ---- 生命周期附属 ----
    request_id: str = ""
    http_status: int = 0
    polling_rounds: int = 0
    debug_responses: list[dict] = field(default_factory=list)  # 供排障，不入报告

    # ---- extensions（仅为协议占位，V1.0 恒为 None）----
    extensions_security: Any = None
    extensions_intent: Any = None
    extensions_voice: Any = None
    extensions_coaching: Any = None

    # ---- Trace/成本/时延指标（实测或明确标记未测量）----
    usage_measured: bool = False

    # ---- 校验 ----
    def validate(self) -> None:
        """结构自检：schema_version 不可省略、状态与决策值合法。非法抛 ValueError。"""
        if not self.schema_version:
            raise ValueError("schema_version 不可省略")
        if self.current_status not in (STATUS_RUNNING, STATUS_SUSPENDED, STATUS_COMPLETED):
            raise ValueError(f"非法 current_status: {self.current_status!r}")
        if self.final_decision not in (
            DECISION_PENDING, DECISION_APPROVED, DECISION_REJECTED, DECISION_SYSTEM_FAILURE,
        ):
            raise ValueError(f"非法 final_decision: {self.final_decision!r}")

    # ---- 序列化 ----
    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "target_type": self.target_type,
            "case_id": self.case_id,
            "trace_id": self.trace_id,
            "business": {
                "current_status": self.current_status,
                "final_decision": self.final_decision,
                "refund_status": self.refund_status,
                "current_graph_node": self.current_graph_node,
                "order_verification": self.order_verification,
                "product_consistency": self.product_consistency,
                "price_consistency": self.price_consistency,
            },
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost": self.cost,
                "measured": bool(self.measured or self.usage_measured),
                "agent": self.agent,
                "model": self.model,
                "model_rates": self.model_rates or {},
            },
            "latency": {
                "wall_ms": self.latency_ms,
                "polling_rounds": self.polling_rounds,
            },
            "tools": [],
            "artifacts": [],
            "errors": [],
            "extensions": {
                "security": self.extensions_security,
                "intent": self.extensions_intent,
                "voice": self.extensions_voice,
                "coaching": self.extensions_coaching,
            },
        }


def extensions_field(name: str) -> str:
    """internal: extensions 命名空间名 -> dataclass 字段名。"""
    return f"extensions_{name}"


def from_dict(payload: dict) -> CandidateOutput:
    """规范化评测侧响应为 CandidateOutput。缺失/非法字段照实报错，不伪造默认值。

    短期容纳两种来源：被测 SUT 的 /v1/evaluation/run 直接返回本结构，或
    judge/runner 从 CaseDetail 聚合生成。字段缺失即缺失，由调用方决定如何处置。
    """
    biz = payload.get("business") or {}
    usage = payload.get("usage") or {}
    lat = payload.get("latency") or {}
    ext = payload.get("extensions") or {}
    out = CandidateOutput(
        schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
        target_type=str(payload.get("target_type") or TARGET_TYPE),
        case_id=str(payload.get("case_id") or ""),
        trace_id=str(payload.get("trace_id") or ""),
        current_status=str(biz.get("current_status") or STATUS_RUNNING),
        final_decision=str(biz.get("final_decision") or DECISION_PENDING),
        refund_status=str(biz.get("refund_status") or REFUND_NONE),
        current_graph_node=str(biz.get("current_graph_node") or "intake"),
        order_verification=str(biz.get("order_verification") or ""),
        product_consistency=str(biz.get("product_consistency") or ""),
        price_consistency=str(biz.get("price_consistency") or ""),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost=float(usage.get("cost") or 0.0),
        usage_measured=bool(usage.get("measured")),
        agent=str(usage.get("agent") or "business"),
        model=usage.get("model"),
        model_rates=usage.get("model_rates") or None,
        latency_ms=float(lat.get("wall_ms") or 0.0),
        polling_rounds=int(lat.get("polling_rounds") or 0),
        http_status=int(payload.get("http_status") or 0),
        request_id=str(payload.get("request_id") or ""),
        extensions_security=ext.get("security"),
        extensions_intent=ext.get("intent"),
        extensions_voice=ext.get("voice"),
        extensions_coaching=ext.get("coaching"),
    )
    out.validate()
    return out


def empty_extensions() -> dict:
    """扩展命名空间默认占位（所有命名空间为 None = 未实现，协议占位）。"""
    return {name: None for name in EXT_NAMESPACES}