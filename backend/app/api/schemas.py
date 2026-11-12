# -*- coding: utf-8 -*-
"""接口 Schema（docs/06 输入输出格式，Pydantic 全量校验）。"""
from decimal import Decimal
from datetime import datetime

from pydantic import BaseModel, Field


# ---- 认证 ----
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    token: str
    expires_in: int
    username: str
    role: str


# ---- 工单 ----
class RefundCreateResponse(BaseModel):
    case_id: str
    status: str = "RUNNING"


class CaseListItem(BaseModel):
    case_id: str
    order_id: str
    requested_amount: Decimal
    current_status: str
    final_decision: str
    created_at: datetime


class CaseListResponse(BaseModel):
    total: int
    items: list[CaseListItem]


class AgentTrace(BaseModel):
    agent_name: str
    status: str
    duration_ms: int | None = None
    error_message: str | None = None
    step_order: int | None = None
    output: dict | None = None  # 各节点业务摘要（核验/欺诈/决策/退款），Trace 展示
    # ---- Phase O：贯穿 trace_id / 真实耗时 / Tool Trace / 模型 ----
    trace_id: str | None = None
    model: str | None = None
    tools: list[dict] | None = None   # Tool Trace 结构（tool_name/risk_level/status/latency_ms/side_effect）


class RefundTx(BaseModel):
    """退款回执（3.3.6）：流水号/金额/状态/时间，详情页展示。"""
    refund_id: str | None
    status: str
    amount: Decimal
    error: str | None = None
    created_at: datetime | None = None


class DecisionScoreDimension(BaseModel):
    key: str
    label: str
    score: int | None = None
    level: str
    direction: str
    explanation: str
    reasons: list[str] = Field(default_factory=list)


class DecisionScores(BaseModel):
    version: str
    dimensions: list[DecisionScoreDimension]


class CaseDetail(BaseModel):
    case_id: str
    order_id: str
    requested_amount: Decimal
    refund_reason: str | None = None
    current_status: str
    final_decision: str
    refund_status: str = "NONE"
    current_graph_node: str | None = None
    fraud_score: int | None = None
    sentiment_level: str | None = None
    ocr_summary: dict | None = None
    decision_scores: DecisionScores
    # ---- 订单/商品/价格核验结果展示（verdict + 理由）----
    order_verification: str | None = None
    order_verify_reasons: list[str] = []
    product_verification: str | None = None
    product_verify_reasons: list[str] = []
    price_verification: str | None = None
    price_verify_reasons: list[str] = []
    # ---- 3.3.6：退款回执一览（详情页/审计可见流水号与来源）----
    refund_transactions: list[RefundTx] = []
    # ---- 评测关联（M8 隔离按 eval_run_id 过滤）----
    eval_run_id: str | None = None
    eval_case_id: str | None = None
    assignee_id: str | None = None
    approval_opinion: str | None = None
    created_at: datetime
    agents: list[AgentTrace] = []


class DecisionRequest(BaseModel):
    opinion: str = Field(min_length=1, max_length=500)


# ---- Dashboard ----
class DashboardStats(BaseModel):
    total: int
    running: int
    suspended: int
    completed: int


# ---- 统一错误（docs/06 ）----
class ErrorResponse(BaseModel):
    code: str
    message: str


# ---- 高级检索（3.3.2）----
class SearchResultItem(BaseModel):
    case_id: str
    order_id: str
    order_no: str | None = None
    requested_amount: Decimal
    refund_reason: str | None = None
    current_status: str
    final_decision: str
    channel: str
    assignee_id: str | None = None
    fraud_score: int | None = None
    sentiment_level: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SearchResponse(BaseModel):
    total: int
    items: list[SearchResultItem]


class SavedSearchItem(BaseModel):
    id: str
    name: str
    filters: dict


class SavedSearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filters: dict = Field(default_factory=dict)


# ---- 基线 V2.0（退款状态独立查询）----
class RefundEventItem(BaseModel):
    """退款状态事件（独立事件流，add-only 审计）。"""
    guid: str
    event_type: str
    previous_status: str
    new_status: str
    gateway_before: str | None = None
    reason: str | None = None
    created_at: str | None = None


class RefundStatusResponse(BaseModel):
    case_id: str
    refund_status: str = "NONE"
    last_guid: str | None = None
    events: list[RefundEventItem] = []


class OrderLookupItem(BaseModel):
    id: str
    order_id: str
    exists: bool
    owner_locked: bool
    trade_status: str | None = None
    refundable: bool
    lookup_kind: str = "api"
    gate_result: str = "PASS"
    reason_codes: list[str] = []
    raw_sha256: str
    created_at: str | None = None


class OrderVerifyResponse(BaseModel):
    case_id: str
    served: bool = True
    snapshot: OrderLookupItem | None = None


class CheckpointViewResponse(BaseModel):
    case_id: str
    has_checkpoint: bool = False
    checkpoint_id: str | None = None
    status: str
    final_decision: str
    next_nodes: list[str] = []   # snapshot.next 节点名（不含上下文/敏感字段）


class ReasonConfirmRequest(BaseModel):
    action: str  #  APPROVED / REJECTED / SUPPLEMENT
    opinion: str = Field(min_length=1, max_length=500)
    refund_status: str | None = None   # 主管确认的退款终态（仅 APPROVED 可指定 SUCCESS）
