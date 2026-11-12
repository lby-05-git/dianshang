# -*- coding: utf-8 -*-
"""领域模型（docs/06 六张表，MySQL 8.0，SQLAlchemy 2.0 风格）。"""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DECIMAL,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---- 状态枚举（docs/06 三态 + 决策终态分离）----
CASE_STATUS_RUNNING = "RUNNING"
CASE_STATUS_SUSPENDED = "SUSPENDED"
CASE_STATUS_COMPLETED = "COMPLETED"

DECISION_PENDING = "PENDING"
DECISION_APPROVED = "APPROVED"
DECISION_REJECTED = "REJECTED"
DECISION_SYSTEM_FAILURE = "SYSTEM_FAILURE"

OCR_STATUS_PENDING = "PENDING"
OCR_STATUS_SUCCESS = "SUCCESS"
OCR_STATUS_UNRELIABLE = "UNRELIABLE"
OCR_STATUS_FAILED = "FAILED"

ROLE_CUSTOMER_SERVICE = "customer_service"
ROLE_SUPERVISOR = "supervisor"
ROLE_LEADER = "leader"
ROLE_ADMIN = "admin"

# ---- 退款回执状态机（不回滚三态）----
REFUND_STATUS_NONE = "NONE"
REFUND_STATUS_PENDING = "PENDING"
REFUND_STATUS_SUCCESS = "SUCCESS"
REFUND_STATUS_FAILED = "FAILED"

# ---- 渠道（3.3.4/3.3.6 策略按渠道覆盖）----
CHANNEL_APP = "APP"
CHANNEL_MINI = "小程序"
CHANNEL_CS = "客服代提"
CHANNEL_DEFAULT = CHANNEL_APP

# ---- 数据范围（3.2.2）----
DATA_SCOPE_SELF = "self"
DATA_SCOPE_TEAM = "team"
DATA_SCOPE_STORE = "store"
DATA_SCOPE_ALL = "all"   # Admin


def new_uuid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    """用户表（docs/06 ）"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # customer_service / supervisor / leader / admin
    org_id: Mapped[str | None] = mapped_column(String(36), default=lambda: "default_org")
    team_id: Mapped[str | None] = mapped_column(String(36))
    data_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DATA_SCOPE_SELF
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class RefundCase(Base):
    """工单主表（docs/06 ）"""

    __tablename__ = "refund_cases"
    __table_args__ = (
        Index("idx_status", "current_status"),
        Index("idx_assignee", "assignee_id"),
        Index("idx_status_sla", "current_status", "sla_deadline_at"),
        Index("idx_assignee_status", "assignee_id", "current_status", "updated_at"),
        Index("idx_org_created", "org_id", "created_at"),
        Index("idx_order_no", "order_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    order_no: Mapped[str | None] = mapped_column(String(64))
    org_id: Mapped[str | None] = mapped_column(String(36), default=lambda: "default_org")
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=CHANNEL_DEFAULT)
    reason_codes: Mapped[list | None] = mapped_column(JSON)
    requested_amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    refund_reason: Mapped[str | None] = mapped_column(Text)
    submitter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    current_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CASE_STATUS_RUNNING
    )
    final_decision: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DECISION_PENDING
    )
    current_graph_node: Mapped[str | None] = mapped_column(String(50))
    fraud_score: Mapped[int | None] = mapped_column(Integer)
    sentiment_level: Mapped[str | None] = mapped_column(String(10))  # LOW/MEDIUM/HIGH
    ocr_summary: Mapped[dict | None] = mapped_column(JSON)
    assignee_id: Mapped[str | None] = mapped_column(String(36))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sla_deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    refund_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REFUND_STATUS_NONE
    )
    trace_id: Mapped[str | None] = mapped_column(String(64))
    approval_opinion: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    # ---- 订单/商品/价格核验（；verification_stage 写入，宽松列向后兼容）----
    order_verification: Mapped[str | None] = mapped_column(String(20))           # MATCH/MISMATCH/UNKNOWN
    order_verification_reasons: Mapped[dict | None] = mapped_column(JSON)        # [str]
    product_verification: Mapped[str | None] = mapped_column(String(20))
    product_verification_reasons: Mapped[dict | None] = mapped_column(JSON)      # [str]
    price_verification: Mapped[str | None] = mapped_column(String(20))
    price_verification_reasons: Mapped[dict | None] = mapped_column(JSON)        # [str]

    # ---- 评测关联（V1.0 评测打标：eval 数据从业务看板/审计过滤）----
    eval_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    eval_case_id: Mapped[str | None] = mapped_column(String(64))

    # ---- 基线 V1.0（最小融合）：意图路由 + 输入安全网关结果 ----
    intent: Mapped[str | None] = mapped_column(String(32))           # REFUND/QUERY/EVIDENCE/APPEAL/HUMAN/COMPLAINT_ESCALATE
    gateway_result: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PASS"            # PASS/BLOCK/REVIEW
    )

    # ---- 基线 V2.0（订单/退款产品化）：事件挂链 + 核验快照 ----
    refund_events_guid: Mapped[str | None] = mapped_column(String(64))   # 最近退款事件 GUID（幂等重放）
    last_order_snapshot_id: Mapped[str | None] = mapped_column(String(36))  # 最近订单沙箱快照

    @classmethod
    def non_eval(cls):
        """业务查询过滤条件：排除评测打标案件（5.2 业务与评测隔离）。

        正常运营看板/列表默认排除 eval_run_id IS NULL 之外的评测数据；
        评测数据只按明确 Run 标识清理（不模糊删除业务工单）。
        """
        return cls.eval_run_id.is_(None)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Evidence(Base):
    """凭证表（docs/06 ）"""

    __tablename__ = "evidences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_md5: Mapped[str | None] = mapped_column(String(32))
    file_phash: Mapped[str | None] = mapped_column(String(64))
    ocr_result: Mapped[dict | None] = mapped_column(JSON)
    ocr_status: Mapped[str] = mapped_column(String(20), default=OCR_STATUS_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentExecutionLog(Base):
    """Agent 轨迹表（docs/06 ）"""

    __tablename__ = "agent_execution_logs"
    __table_args__ = (Index("idx_case", "case_id", "step_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS/FAILED/SKIPPED
    input_snapshot: Mapped[dict | None] = mapped_column(JSON)
    output_snapshot: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    step_order: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    # Phase O：Tool Trace 结构 + 业务模型标识（宽松列，向后兼容，历史行取空）
    trace: Mapped[dict | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class HumanIntervention(Base):
    """人工审批表（docs/06 ）"""

    __tablename__ = "human_interventions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    approver_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str | None] = mapped_column(String(20))  # APPROVED / REJECTED
    opinion: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SystemConfig(Base):
    """系统配置表（docs/06 ），阈值运行期可热改"""

    __tablename__ = "system_configs"

    cfg_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    cfg_value: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Team(Base):
    """班组（3.3.1 协同台 / 3.2.2 数据范围）"""

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TeamMember(Base):
    """班组-成员关联（3.3.1）"""

    __tablename__ = "team_members"
    __table_args__ = (Index("idx_team_member_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PolicyProfile(Base):
    """策略档（默认值 = MVP）"""

    __tablename__ = "policy_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_threshold: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False, default=300)
    ocr_confidence: Mapped[float] = mapped_column(Integer, nullable=False, default=60)  # 存整数百分比 0-100，默认 60(=0.6)
    fraud_pass: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    fraud_suspend: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    blacklist_autoreject: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    sla_remind_hr: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    sla_escalate_hr: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    is_default: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PolicyBinding(Base):
    """策略档-渠道绑定（3.3.4）"""

    __tablename__ = "policy_bindings"
    __table_args__ = (Index("idx_policy_binding_profile", "profile_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("policy_profiles.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ComplaintNote(Base):
    """内部批注（不对客；@ 触发站内提醒）"""

    __tablename__ = "complaint_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    author_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentioned_user_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DomainEvent(Base):
    """领域事件（append-only 审计；只 insert 不改删）"""

    __tablename__ = "domain_events"
    __table_args__ = (
        Index("idx_domain_event_actor", "actor_id"),
        Index("idx_domain_event_case", "case_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(String(36))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RefundTransaction(Base):
    """退款请求与回执（status 状态机不回滚三态）"""

    __tablename__ = "refund_transactions"
    __table_args__ = (Index("idx_refund_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    refund_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=REFUND_STATUS_PENDING
    )
    amount: Mapped[Decimal] = mapped_column(DECIMAL(10, 2), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SavedSearch(Base):
    """用户筛选器（3.3.2 高级检索保存）"""

    __tablename__ = "saved_searches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    filters: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class KpiDailyRollup(Base):
    """看板日汇总（可异步生成）"""

    __tablename__ = "kpi_daily_rollups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rollup_date: Mapped[str] = mapped_column(String(20), nullable=False)  # YYYY-MM-DD
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default=CHANNEL_DEFAULT)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_approve_rate: Mapped[float] = mapped_column(nullable=False, default=0)  # 秒退率
    suspend_rate: Mapped[float] = mapped_column(nullable=False, default=0)
    reject_rate: Mapped[float] = mapped_column(nullable=False, default=0)
    human_p50_ms: Mapped[int | None] = mapped_column(Integer)
    human_p95_ms: Mapped[int | None] = mapped_column(Integer)
    completed_per_agent: Mapped[float] = mapped_column(nullable=False, default=0)  # 人均结案
    agent_fallback_rate: Mapped[float] = mapped_column(nullable=False, default=0)
    ocr_low_conf_rate: Mapped[float] = mapped_column(nullable=False, default=0)
    sla_breach_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# 评测中心（V2.0 / AGENTS.md 版本路线）
# 逻辑对象：Evaluation Suite / Case / Run / Result / Artifact /
# Prompt Version。评测数据与业务数据隔离；版本不可变；Run 关联版本。
# =====================================================================

# ---- 评测 Run 状态----
EVAL_RUN_PENDING = "PENDING"
EVAL_RUN_RUNNING = "RUNNING"
EVAL_RUN_COMPLETED = "COMPLETED"
EVAL_RUN_FAILED = "FAILED"
EVAL_RUN_CANCELLED = "CANCELLED"

# ---- 单 Case 结果状态（；SKIPPED_UNSUPPORTED 用于 Target 未声明能力）----
EVAL_RESULT_PASS = "PASS"
EVAL_RESULT_FAIL = "FAIL"
EVAL_RESULT_ERROR = "ERROR"
EVAL_RESULT_SKIPPED_UNSUPPORTED = "SKIPPED_UNSUPPORTED"

# ---- 数据集/套件状态 ----
EVAL_SUITE_DRAFT = "DRAFT"
EVAL_SUITE_ACTIVE = "ACTIVE"
EVAL_SUITE_ARCHIVED = "ARCHIVED"

# ---- Prompt 版本批准状态 ----
PROMPT_DRAFT = "DRAFT"
PROMPT_CANDIDATE = "CANDIDATE"
PROMPT_APPROVED = "APPROVED"
PROMPT_REJECTED = "REJECTED"

# ---- 评测分级调度（M18 V3.0 / V3.2 六类任务）----
# 六类任务层（-评测）：smoke 冒烟 / core 核心（≥100 条） / full 全量 /
# security 安全任务 / voice 语音任务 / intent 意图周期对照。security、voice 为预留能力分层，
# 能力未就绪的用例只会 SKIPPED，绝不当作通过（measured 语义守线）。
EVAL_TIERS = ("smoke", "core", "full", "security", "voice", "intent")
EVAL_SCHEDULE_CADENCE = ("manual", "daily", "weekly", "change")

# ---- V3.0 V3-3 企业级零信任治理（工具风险分级 / 版本注册中心）----
# 工具四级风险类：readonly 只读 / internal_write 内部写 / external_notify 外部通知 /
# fund_effect 资金影响（退款类，要求 策略+HITL+幂等+审计 四同时）。
TOOL_RISK_READONLY = "readonly"
TOOL_RISK_INTERNAL_WRITE = "internal_write"
TOOL_RISK_EXTERNAL_NOTIFY = "external_notify"
TOOL_RISK_FUND_EFFECT = "fund_effect"
TOOL_RISK_CLASSES = (TOOL_RISK_READONLY, TOOL_RISK_INTERNAL_WRITE,
                     TOOL_RISK_EXTERNAL_NOTIFY, TOOL_RISK_FUND_EFFECT)

# 版本注册中心实体类型（统一治理：Agent/Prompt/模型/工具/权限/数据集/安全策略）
VERSION_ENTITY_TYPES = ("agent", "prompt", "model", "tool", "permission",
                        "dataset", "security_policy")
VERSION_STATUS_DRAFT = "DRAFT"
VERSION_STATUS_CANDIDATE = "CANDIDATE"
VERSION_STATUS_APPROVED = "APPROVED"
VERSION_STATUS_REJECTED = "REJECTED"
VERSION_STATUS_ROLLED_BACK = "ROLLED_BACK"
VERSION_STATUSES = (VERSION_STATUS_DRAFT, VERSION_STATUS_CANDIDATE,
                    VERSION_STATUS_APPROVED, VERSION_STATUS_REJECTED,
                    VERSION_STATUS_ROLLED_BACK)


class EvalSuite(Base):
    """数据集（Evaluation Suite / 版本不可变）。

    基线版本只读；修改通过复制创建新版本（snapshot JSON 存不可变基线）。
    """

    __tablename__ = "eval_suites"
    __table_args__ = (Index("idx_eval_suite_name", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)     # 如 "3.0.0"
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    tags: Mapped[dict | None] = mapped_column(JSON)                       # 标签列表
    modalities: Mapped[dict | None] = mapped_column(JSON)
    required_capabilities: Mapped[dict | None] = mapped_column(JSON)
    tier_map: Mapped[dict | None] = mapped_column(JSON)                  # M18：{tier: [case ids]}
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)          # 不可变基线文档
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EVAL_SUITE_ACTIVE
    )
    owner_id: Mapped[str | None] = mapped_column(String(36))
    changelog: Mapped[str | None] = mapped_column(Text)                  # 变更说明
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalCase(Base):
    """评测 Case（Evaluation Case）：数据集内单条的不可变快照。"""

    __tablename__ = "eval_cases"
    __table_args__ = (
        Index("idx_eval_case_suite", "suite_id"),
        Index("idx_eval_case_ref", "suite_id", "case_ref_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id"), nullable=False
    )
    case_ref_id: Mapped[str] = mapped_column(String(20), nullable=False)  # G01..G30
    case_type: Mapped[str | None] = mapped_column(String(50))
    tags: Mapped[dict | None] = mapped_column(JSON)
    category: Mapped[str | None] = mapped_column(String(20))              # V2.0 core/boundary/anomaly/concurrency/security/voice
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False)            # order_id/amount/reason...
    expected: Mapped[dict] = mapped_column(JSON, nullable=False)          # 预期业务事实
    constraints: Mapped[dict | None] = mapped_column(JSON)                # allow/max_wait/nodes/side_effect
    extensions: Mapped[dict | None] = mapped_column(JSON)                 # security/intent/voice/coaching 预留
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalRun(Base):
    """评测 Run（Evaluation Run）：异步任务 + 汇总结果。

    task envelope 字段：task_type/schema_version/priority/trace_id/run_id/case_id/extensions；
    不实现 security/intent/voice/coaching 的实际业务（只保留 task_type 预留值）。
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("idx_eval_run_status", "status"),
        Index("idx_eval_run_suite", "suite_id"),
        Index("idx_eval_run_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # run-xxxx
    name: Mapped[str | None] = mapped_column(String(200))
    task_type: Mapped[str] = mapped_column(String(30), nullable=False, default="refund_eval")
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EVAL_RUN_PENDING
    )
    suite_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("eval_suites.id")
    )
    suite_version: Mapped[str | None] = mapped_column(String(20))
    dataset_checksum: Mapped[str | None] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(30))
    base_url: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(36))             # user_id
    config: Mapped[dict | None] = mapped_column(JSON)                      # limit/use_llm/timeout/concurrency
    environment: Mapped[dict | None] = mapped_column(JSON)                # python/base_url
    summary: Mapped[dict | None] = mapped_column(JSON)                    # total/passed/failed/degraded
    performance: Mapped[dict | None] = mapped_column(JSON)                # p50/p95
    cost: Mapped[dict | None] = mapped_column(JSON)                       # tokens/cost/measured 分账
    prompt_variant_id: Mapped[str | None] = mapped_column(String(36))
    prompt_baseline_id: Mapped[str | None] = mapped_column(String(36))
    trigger_source: Mapped[str | None] = mapped_column(String(20))         # V2.0 code/prompt/model/policy/manual
    report_path: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(Text)                       # FAILED 原因/停止信息
    trace_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalCaseResult(Base):
    """单 Case 结果（Evaluation Result）。

    result 状态独立于业务：PASS/FAIL/ERROR/SKIPPED_UNSUPPORTED。
    judge 与 candidate 保持报告 schema 1.0（历史 V1 报告兼容读取）。
    """

    __tablename__ = "eval_case_results"
    __table_args__ = (
        Index("idx_eval_case_result_run", "run_id"),
        Index("idx_eval_case_result_case", "run_id", "case_ref_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)
    case_ref_id: Mapped[str] = mapped_column(String(20), nullable=False)   # G01..G30
    case_type: Mapped[str | None] = mapped_column(String(50))
    verdict: Mapped[str] = mapped_column(
        String(30), nullable=False, default=EVAL_RESULT_ERROR
    )                                   # PASS/FAIL/ERROR/SKIPPED_UNSUPPORTED
    score: Mapped[float | None]
    dimensions: Mapped[dict | None] = mapped_column(JSON)                  # [DimensionResult]
    evidence: Mapped[dict | None] = mapped_column(JSON)                    # [str]
    judge_version: Mapped[str | None] = mapped_column(String(10))
    degraded: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    degraded_reason: Mapped[str | None] = mapped_column(Text)
    execution: Mapped[dict | None] = mapped_column(JSON)                   # mode/latency/polling
    candidate: Mapped[dict | None] = mapped_column(JSON)                   # CandidateOutput（business+usage+extensions）
    llm_judge: Mapped[dict | None] = mapped_column(JSON)                   # 可选 LLM 附加维度
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalArtifact(Base):
    """评测附带文件（Artifact）。

    mime_type 通用化；sensitivity 用于后续安全/语音；storage_ref 指向上传/生成文件。
    """

    __tablename__ = "eval_artifacts"
    __table_args__ = (
        Index("idx_eval_artifact_run", "run_id"),
        Index("idx_eval_artifact_case", "run_id", "case_ref_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str | None] = mapped_column(String(40))
    case_ref_id: Mapped[str | None] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(30), nullable=False)          # dataset_import / report / evidence
    mime_type: Mapped[str] = mapped_column(String(80), nullable=False)     # 如 application/vnd.ms-excel
    checksum: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    storage_ref: Mapped[str | None] = mapped_column(String(300))          # 相对路径
    sensitivity: Mapped[str] = mapped_column(String(20), nullable=False, default="internal")
    approved: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PromptVersion(Base):
    """Prompt 版本（Prompt Version）。

    候选版本必须人工批准后才可进入对比/生产；内容变更即新版本。
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (Index("idx_prompt_version_agent", "agent_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False)     # 如 sentiment
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[Text] = mapped_column(Text, nullable=False)
    content_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PROMPT_DRAFT
    )                                     # DRAFT/CANDIDATE/APPROVED/REJECTED
    creator_id: Mapped[str | None] = mapped_column(String(36))
    approver_id: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    changelog: Mapped[str | None] = mapped_column(Text)
    is_baseline: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalSchedule(Base):
    """评测调度（M18 V3.0 持续评测治理）。

    一个 Dataset Suite 可按分层（tier）绑定一个调度：手动 / 每日 / 每周 / 变更。
    调度只负责"到期触发 Run"——绝不自动改任何决策；绑定非 APPROVED Prompt 直接拒绝。
    last_fired / 幂等由 unique(suite_id, tier) 保证不重复触发。
    """

    __tablename__ = "eval_schedules"
    __table_args__ = (
        Index("idx_eval_schedule_suite", "suite_id"),
        Index("uq_eval_schedule_suite_tier", "suite_id", "tier", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id"), nullable=False
    )
    tier: Mapped[str] = mapped_column(String(20), nullable=False)          # smoke/core/full
    cadence: Mapped[str] = mapped_column(String(20), nullable=False)       # manual/daily/weekly/change
    enabled: Mapped[bool] = mapped_column(server_default="1", nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_id: Mapped[str | None] = mapped_column(String(40))    # 最近一次触发 Run
    owner_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalBaseline(Base):
    """发布基线（M19 V3.0 发布门禁）。

    人工把某个 COMPLETED Run 批准为套件发布基线；approver/approved_at 留痕。
    一个 role（默认 "default"）基线唯一：切换基线只可能指向证据链中的运行。
    """

    __tablename__ = "eval_baselines"
    __table_args__ = (
        Index("idx_eval_baseline_suite_role", "suite_id", "role"),
        UniqueConstraint("suite_id", "role", name="uq_eval_baseline_suite_role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="default")
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)   # 证据链中的 Run
    dataset_checksum: Mapped[str | None] = mapped_column(String(64))
    suite_version: Mapped[str | None] = mapped_column(String(20))
    approved_by: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalGateResult(Base):
    """发布门禁判定记录（M19 V3.0 ）。

    每次对候选 Run 的门禁判定证据持久化（decision/passed/detail JSON），
    供治理页展示与审计。判定只读：不修改候选/基线 Run、不修改数据集。
    """

    __tablename__ = "eval_gate_results"
    __table_args__ = (Index("idx_eval_gate_run", "run_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    gate_id: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)      # 候选 Run
    baseline_run_id: Mapped[str | None] = mapped_column(String(40))
    suite_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)    # PASS/BLOCKED_*
    passed: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)                    # GateResult.as_dict()
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalBudget(Base):
    """评测成本预算（M20 V3.0 持续评测治理）。

    按维度（env/model/prompt/agent/task_type/run 汇总）设置预算上限；
    预算只告警，绝不自动改退款/评测决策。period: daily/weekly/monthly。
    """

    __tablename__ = "eval_budgets"
    __table_args__ = (
        Index("idx_eval_budget_dim", "dimension", "dim_key"),
        UniqueConstraint("dimension", "dim_key", "period",
                         name="uq_eval_budget_dim_period"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    dimension: Mapped[str] = mapped_column(String(30), nullable=False)   # env/model/prompt/agent/task_type/run
    dim_key: Mapped[str] = mapped_column(String(80), nullable=False)     # 维度具体值（如 URL / model 名 / prompt id）
    period: Mapped[str] = mapped_column(String(10), nullable=False)      # daily/weekly/monthly
    limit_amount: Mapped[float] = mapped_column(server_default="100.0")
    limit_tokens: Mapped[int] = mapped_column(server_default="1000000")
    enabled: Mapped[bool] = mapped_column(server_default="1", nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalDriftIndicator(Base):
    """漂移监测指标（M20 V3.0 持续评测治理）。

    每次 Run 结束由 Worker 收集：错误率（degraded+failed）/P95/通过率，
    与历史窗口基线对比标记异常（drifted）。只记录与告警，不改退款决策。
    """

    __tablename__ = "eval_drift_indicators"
    __table_args__ = (Index("idx_eval_drift_run", "run_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)
    suite_id: Mapped[str | None] = mapped_column(String(36))
    metric_key: Mapped[str] = mapped_column(String(40), nullable=False)  # error_rate/p95/pass_rate
    value: Mapped[float] = mapped_column(server_default="0")
    baseline: Mapped[float] = mapped_column(server_default="0")
    threshold: Mapped[float] = mapped_column(server_default="0")
    drifted: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EvalAlert(Base):
    """评估告警（M20 V3.0 ）。漂移 / 预算超支触发。

    只告警，绝不自动改退款/评测决策；ack 后不再重复提示同一 run/metric。
    """

    __tablename__ = "eval_alerts"
    __table_args__ = (Index("idx_eval_alert_run", "run_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    alert_type: Mapped[str] = mapped_column(String(30), nullable=False)  # DRIFT / BUDGET
    run_id: Mapped[str | None] = mapped_column(String(40))
    suite_id: Mapped[str | None] = mapped_column(String(36))
    metric_key: Mapped[str] = mapped_column(String(40), nullable=False)  # drift metric / budget dimension
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="warning")
    acked: Mapped[bool] = mapped_column(server_default="0", nullable=False)
    acked_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# 基线 V1.0（扩展能力最小融合）
# 迁移：e2f3a4b5c6d7（纯 add-only，不动既有表语义；EVAL 隔离保持）
# =====================================================================

# ---- 意图路由 ----
INTENT_REFUND = "REFUND"
INTENT_QUERY = "QUERY"
INTENT_EVIDENCE = "EVIDENCE"
INTENT_APPEAL = "APPEAL"
INTENT_HUMAN = "HUMAN"
INTENT_COMPLAINT_ESCALATE = "COMPLAINT_ESCALATE"
INTENT_UNKNOWN = "UNKNOWN"

# ---- 输入安全网关 ----
GATE_PASS = "PASS"
GATE_BLOCK = "BLOCK"
GATE_REVIEW = "REVIEW"

# ---- 语音会话状态 ----
VOICE_LISTENING = "LISTENING"
VOICE_STT = "STT"
VOICE_LLM = "LLM"
VOICE_TTS = "TTS"
VOICE_INTERRUPTED = "INTERRUPTED"
VOICE_ENDED = "ENDED"

VOICE_SCENE_REFUND = "refund_service_lt1"       # 仅 1 个客诉退赔陪练场景（常量）

# ---- DLP 脱敏类型 ----
DLP_PHONE = "PHONE"
DLP_ID_CARD = "ID_CARD"
DLP_BANK_CARD = "BANK_CARD"
DLP_API_KEY = "API_KEY"

# ---- 陪练评分维度（5 维，仅培训建议）----
COACHING_DIMS = ("professional", "empathy", "compliance", "resolution", "efficiency")


class IntentResult(Base):
    """两级意图路由留存。

    只落原文 SHA-256，不落敏感明文；规则层命中则 level_1_rule 非空，
    LLM 层不可用时 level_2_llm 为 None（Fail-Safe 由调用方转 HUMAN）。
    V2.0（-4）：conflict_candidates 存多意图候选，route_source 存规则/LLM来源。
    """

    __tablename__ = "intent_results"
    __table_args__ = (Index("idx_intent_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    level_1_rule: Mapped[str | None] = mapped_column(String(32))       # 规则层命中意图
    level_2_llm: Mapped[str | None] = mapped_column(String(32))        # LLM 层意图
    final_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, server_default="LOW")
    conflict_candidates: Mapped[dict | None] = mapped_column(JSON)     # V2.0 多意图候选
    route_source: Mapped[str | None] = mapped_column(String(16))       # V2.0 rule/llm/hybrid
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SecurityDecision(Base):
    """输入安全网关 / 退款工具门禁判定。

    只存脱敏文本（masked_input）；source 区分 intake/evidence/refund_tool/voice；
    tool_name 在 source=refund_tool 时为 execute_refund 等。判定只记录，不改业务语义。
    """

    __tablename__ = "security_decisions"
    __table_args__ = (Index("idx_security_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # case_id 可空（迁移 f3e4d5c6b7a9）：llm-critic 独立检查无案件归属时以审计行留存
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refund_cases.id")
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    gate_result: Mapped[str] = mapped_column(String(16), nullable=False)  # PASS/BLOCK/REVIEW
    injection_score: Mapped[float | None] = mapped_column()               # 越狱/注入风险概率
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)   # LOW/MEDIUM/HIGH
    masked_input: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64))
    rule_hits: Mapped[dict | None] = mapped_column(JSON)                  # 命中的注入特征 / DLP 规则
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DlpFinding(Base):
    """DLP 正则脱敏命中明细"""

    __tablename__ = "dlp_findings"
    __table_args__ = (Index("idx_dlp_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    finding_type: Mapped[str] = mapped_column(String(24), nullable=False)  # PHONE/ID_CARD/BANK_CARD/API_KEY
    position: Mapped[int] = mapped_column(Integer, nullable=False)         # 原文偏移
    plain_snippet: Mapped[str] = mapped_column(String(64), nullable=False) # 命中片段（落库前掩码）
    masked: Mapped[str] = mapped_column(String(64), nullable=False)        # 掩码结果示例
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class VoiceSession(Base):
    """单会话语音闭环（WS + Mock VAD/STT/TTS）。

    created_case_id：语音识别确认关键信息（金额/订单号）后创建的测试工单（EVAL 打标）。
    tts_cleared：插话即停触发次数（物理打断指标）。
    """

    __tablename__ = "voice_sessions"
    __table_args__ = (UniqueConstraint("session_id", name="uq_voice_session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refund_cases.id")
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)  # LISTENING/STT/LLM/TTS/INTERRUPTED/ENDED
    stt_text: Mapped[str | None] = mapped_column(Text)
    tts_cleared: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_case_id: Mapped[str | None] = mapped_column(String(36))
    reconnects: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # V2.0
    weak_signal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")  # V2.0
    closed_reason: Mapped[str | None] = mapped_column(String(30))     # V2.0 timeout/reconnect/end
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)


class CoachingSession(Base):
    """客服陪练 1 场景。

    user_id 仅用于隔离会话；turns 不落敏感原文（已脱敏）；score_json 5 维；
    suggestion 仅培训建议文字，无任何资金/退款动作（禁副作用红线）。
    """

    __tablename__ = "coaching_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_id: Mapped[str] = mapped_column(String(64), nullable=False)  # VOICE_SCENE_REFUND
    difficulty: Mapped[str | None] = mapped_column(String(20))         # V2.0 easy/medium/hard
    emotion: Mapped[str | None] = mapped_column(String(20))            # V2.0 calm/upset/escalated
    scene_name: Mapped[str | None] = mapped_column(String(64))         # V2.0 多场景名称
    turns: Mapped[dict | None] = mapped_column(JSON)                   # 回合记录（脱敏后）
    score_json: Mapped[dict] = mapped_column(JSON, nullable=False)     # 5 维评分
    suggestion: Mapped[str | None] = mapped_column(Text)               # 培训建议
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# 基线 V2.0（：产品化、安全强化与多会话）
# 迁移：f2e3d4c5b6a8（纯 add-only，不动既有表语义；EVAL 隔离保持）
# =====================================================================

# ---- 退款状态事件（-1 独立查询/回执/重试/审计）----
class RefundEvent(Base):
    """退款状态独立事件流：NONE/PENDING/SUCCESS/FAILED。

    guid 唯一保证业务幂等（重放不重复扣款）；gateway_before 记录执行前
    tool_gate 快照（审计）。event_type: QUERY/REPLAY/OK/FAIL/RETRY/EXEC。
    """

    __tablename__ = "refund_events"
    __table_args__ = (
        Index("idx_refund_event_case", "case_id"),
        UniqueConstraint("guid", name="uq_refund_event_guid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    guid: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway_before: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 订单中心沙箱快照（-1 订单真实性产品化）----
class OrderLookupSnapshot(Base):
    """订单中心沙箱查询快照：存在性/归属/交易状态/可退资格。

    raw_sha256 存快照原文哈希（不落敏感明文）；reason_codes 存结构化原因码
    （超时/不存在/归属不符/商品不符/价格不符）。只读佐证，不改业务语义。
    """

    __tablename__ = "order_lookup_snapshots"
    __table_args__ = (
        Index("idx_order_lookup_case", "case_id"),
        Index("idx_order_snap_order", "order_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    exists: Mapped[bool] = mapped_column(Boolean, nullable=False)
    owner_locked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trade_status: Mapped[str | None] = mapped_column(String(20))
    refundable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lookup_kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="sandbox")
    gate_result: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PASS")
    reason_codes: Mapped[dict | None] = mapped_column(JSON)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 安全事件工作台（-3 可追溯 + 处置/审计）----
class SecurityEvent(Base):
    """安全事件：DLP/注入/越权/工具门禁 即录。

    disposition 流转 OPEN→TRIAGE→CONFIRMED/FALSE_POSITIVE→CLOSED；
    audit_sha256 记录事件原文哈希（掩码后详情存 detail）。只记录，不改判定。
    """

    __tablename__ = "security_events"
    __table_args__ = (
        Index("idx_sec_evt_type", "event_type"),
        Index("idx_sec_evt_case", "case_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refund_cases.id")
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, server_default="medium")
    detail: Mapped[str | None] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False, server_default="OPEN")
    disposed_by: Mapped[str | None] = mapped_column(String(36))
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime)
    audit_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 意图纠偏反馈集（-4 线上误路由 → 评测候选）----
class IntentFeedbackSample(Base):
    """线上误路由/人工纠正案例 → 评测候选集。

    PENDING→REVIEWED→ADOPTED (进 golden)/REJECTED；ADOPTED 前绝不影响线上。
    """

    __tablename__ = "intent_feedback_samples"
    __table_args__ = (Index("idx_intent_fb_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refund_cases.id")
    )
    route_final_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    human_corrected_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="PENDING")
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# V3-4 意图模型治理（-意图）：分类器版本 + 回放集
# =====================================================================

# 分类器种类：规则 / 词典 / 轻量模型 / LLM（各自独立版本导出）
CLASSIFIER_RULE = "rule"
CLASSIFIER_DICT = "dict"
CLASSIFIER_LIGHTWEIGHT = "lightweight"
CLASSIFIER_LLM = "llm"
CLASSIFIERS = (CLASSIFIER_RULE, CLASSIFIER_DICT, CLASSIFIER_LIGHTWEIGHT, CLASSIFIER_LLM)

CLS_VERSION_DRAFT = "DRAFT"
CLS_VERSION_APPROVED = "APPROVED"
CLS_VERSION_REJECTED = "REJECTED"
CLS_VERSION_STATUSES = frozenset(
    {CLS_VERSION_DRAFT, CLS_VERSION_APPROVED, CLS_VERSION_REJECTED}
)

# 回放集状态：PENDING（待审核）-> APPROVED（批准列入回放验证）/ SKIPPED（驳回）。
# APPROVED 仅代表「进黄金集重建前的待回放验证候选」；绝不自动改线上路由规则。
REPLAY_PENDING = "PENDING"
REPLAY_APPROVED = "APPROVED"
REPLAY_SKIPPED = "SKIPPED"
REPLAY_STATUSES = frozenset({REPLAY_PENDING, REPLAY_APPROVED, REPLAY_SKIPPED})


class IntentClassifierVersion(Base):
    """分类器版本注册（V3-4）：rule/dict/lightweight/llm 各自独立版本。

    只读注册表：快照（payload）+ sha256 指纹 + 批准留痕。任何版本记录都不
    自动切换线上路由 —— 切换必须走既有发布门禁/人工批准。
    """

    __tablename__ = "intent_classifier_versions"
    __table_args__ = (
        Index("idx_intent_cls_name", "classifier_name"),
        UniqueConstraint("classifier_name", "version", name="uq_intent_cls_name_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    classifier_name: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=CLS_VERSION_DRAFT
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSON)       # 快照：规则集/词典/模型指纹/提示词
    changelog: Mapped[str | None] = mapped_column(Text)
    creator_id: Mapped[str | None] = mapped_column(String(36))
    approver_id: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class IntentReplayQueueItem(Base):
    """意图回放集（V3-4）：feedback ADOPTED → 待审核回放（去重/限长）。

    与 IntentFeedbackSample 1:N（每条 sample 至多一行，sample_id 唯一防重复入队）。
    APPROVED 只标记「列入黄金集重建回放验证候选」，真人审核（golden 冻结流程）
    之后才可能生效 —— 绝不自动改在线路由（V-R1 红线）。
    """

    __tablename__ = "intent_replay_queue"
    __table_args__ = (
        Index("idx_intent_replay_status", "status"),
        UniqueConstraint("sample_id", name="uq_intent_replay_sample"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sample_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("intent_feedback_samples.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=REPLAY_PENDING
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 语音资源/容量基线（-5 并发度量）----
class VoiceMetric(Base):
    """语音会话度量（V3-5 扩增分段）：LATENCY_P99 / ERROR / PEAK_CONCURRENCY / DROP /
    VAD_END / STT / LLM_FIRST / TTS_FIRST（无 schema 变更，metric_kind 纯枚举）。"""

    __tablename__ = "voice_metrics"
    __table_args__ = (Index("idx_voice_metric_kind", "metric_kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[float] = mapped_column(nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 死信任务（-4 查询/修复/人工重放）----
class DeadLetterTask(Base):
    """死信任务：退款/评测/意图 异常的兜底。

    replay_guid 保证重放幂等（不重复退款）；REPLAYED 后置 status=CLOSED。
    """

    __tablename__ = "dead_letter_tasks"
    __table_args__ = (Index("idx_dl_status", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="OPEN")
    replayed_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    replay_guid: Mapped[str | None] = mapped_column(String(64))
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---- 数据集重分类（-2 ≥30 条按类别）----
class DatasetCategoryMap(Base):
    """数据集用例类别映射：core/boundary/anomaly/concurrency/security/voice。

    suite×case 唯一；不改 golden 冻结内容，独立分类而非侵入式改 case。
    """

    __tablename__ = "dataset_category_map"
    __table_args__ = (
        Index("idx_cat_map_cat", "category"),
        UniqueConstraint("suite_id", "case_ref_id", name="uq_dataset_cat_suite_case"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id"), nullable=False
    )
    case_ref_id: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# V3.0（：持续治理与生产级高并发）
# 迁移：a4b5c6d7e8f9（纯 add-only，不动既有表语义）
# =====================================================================

# ---- 对账差异/来源判定状态常量（-订单）----
RECON_SEV_INFO = "INFO"
RECON_SEV_WARNING = "WARNING"
RECON_SEV_HIGH = "HIGH"
RECON_SEV_CRITICAL = "CRITICAL"
RECON_SEVERITIES = (RECON_SEV_INFO, RECON_SEV_WARNING, RECON_SEV_HIGH, RECON_SEV_CRITICAL)
# 可达告警级别（overview / alerts 默认过滤）：HIGH 及以上
RECON_ALERT_SEVERITIES = (RECON_SEV_HIGH, RECON_SEV_CRITICAL)


class ReconciliationEvent(Base):
    """自动对账差异事件。

    对账把「工单/决策/退款」与「订单源/退款回执」做一致性 diff：
      - category: decision / verify / amount / refund_status / price；
      - severity 分级：CRITICAL=触发资金红线（如已 APPROVED+已退而核验未全 MATCH）、
        HIGH=状态与回执不符 / 金额超可退、WARNING/INFO=低风险提示；
      - guid = sha256(run_id:case_id:category:field)，同 run 同差异幂等，重跑不重复落审计；
      - resolved 供主管处置置位（只标记，不删除审计行——audit 一贯只增不改）。
    """

    __tablename__ = "reconciliation_events"
    __table_args__ = (
        Index("idx_recon_case", "case_id"),
        UniqueConstraint("guid", name="uq_recon_event_guid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("refund_cases.id")
    )                                # 可空：对账外 diff（如孤儿退款回执）
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=RECON_SEV_INFO
    )
    expected: Mapped[dict | None] = mapped_column(JSON)
    actual: Mapped[dict | None] = mapped_column(JSON)
    diff_detail: Mapped[str | None] = mapped_column(Text)
    guid: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ToolDefinition(Base):
    """工具风险分级注册表（V3-3 企业级零信任治理：工具按风险分级 + 策略绑定）。

    risk_class 取值 readonly / internal_write / external_notify / fund_effect：
    - readonly           只读查询（order_lookup / external_query / ocr_extract）；
    - internal_write     内部写（document_generate / evidence_upload / 策略保存）；
    - external_notify    外部通知（notification_send，有外部副作用但不动资金）；
    - fund_effect        资金影响（execute_refund 等），要求 策略+HITL+幂等+审计 四同时。

    本表只配置分级与策略绑定；检查层（security_gateway.check_tool_risk_gate）读取
    判定，绝不绕过既有 refund_tool / tool_gate 门禁。fund_effect 且 enabled=False
    时检查层直接拒绝。
    """

    __tablename__ = "tool_definitions"
    __table_args__ = (
        UniqueConstraint("tool_name", name="uq_tool_definitions_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_class: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str | None] = mapped_column(String(120))
    policy_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("policy_profiles.id")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    requires_hitl: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    requires_idempotency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0"
    )
    requires_audit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class VersionRegistry(Base):
    """版本注册中心（V3-3：Agent/Prompt/模型/工具/权限/数据集/安全策略统一版本管理）。

    - entity_type × entity_name × version 唯一（防重）；
    - status 走 DRAFT→CANDIDATE→APPROVED 批准链；REJECTED 为否决；ROLLED_BACK 为回滚
      （rollback_to 记录回滚基准版本号，回滚只有留痕、绝不静默改策略/工具行为）；
    - content_sha256：内容指纹（Prompt/数据集等，供「版本可复现」核对）；
    - payload：版本快照 JSON（仅存脱敏/非敏感元数据，Prompt 全文须走 prompt_versions 表）。
    """

    __tablename__ = "version_registry"
    __table_args__ = (
        Index("idx_ver_reg_entity", "entity_type", "entity_name"),
        UniqueConstraint("entity_type", "entity_name", "version",
                         name="uq_ver_reg_entity_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=VERSION_STATUS_DRAFT
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSON)
    changelog: Mapped[str | None] = mapped_column(Text)
    creator_id: Mapped[str | None] = mapped_column(String(36))
    approver_id: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    rollback_to: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OrderSourceVerdict(Base):
    """多来源订单核验判定留痕（+ 熔断降级）。

    每次 MultiSourceOrderProvider.lookup 对每个来源落一行该来源的可用/判定；
    供对账与审计复盘「某来源是否可信、是否降级」。只写审计，不改订单语义。
    """

    __tablename__ = "order_source_verdicts"
    __table_args__ = (Index("idx_osv_case", "case_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("refund_cases.id"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(30), nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # EXIST/MISSING/UNKNOWN
    reason: Mapped[dict | None] = mapped_column(JSON)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# =====================================================================
# V3.0 V3-6
# 迁移：d7e8f9a0b1c2（纯 add-only，不动既有表语义）
# =====================================================================

# ---- Trace 降级本地队列状态常量（-4：上报失败绝不阻塞退款主流程）----
TRACE_LQ_OPEN = "OPEN"
TRACE_LQ_DRAINED = "DRAINED"
TRACE_LQ_DEAD = "DEAD"


class TraceLocalQueueItem(Base):
    """Trace 上报失败 -> 本地降级队列（V3-6 -4）。

    主 Trace 通道（Redis Stream 追加 / 外部采集）不可用时，把已脱敏的 Trace 记录
    落地 MySQL 本表（MySQL 与 Redis 通道独立，互不叠单点）；`drain_local_queue`
    恢复通道后重放，幂等（delivery_key 唯一）。明文 PII 永不落此表（DLP 红线：
    只存已脱敏 payload）。本表是旁路，绝不影响退款/决策主链路。
    """

    __tablename__ = "trace_local_queue"
    __table_args__ = (
        Index("idx_trace_lq_status", "status", "enqueued_at"),
        UniqueConstraint("delivery_key", name="uq_trace_lq_delivery"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    delivery_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # agent_log / stream_append
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)    # 已脱敏 Trace 记录
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        server_default=TRACE_LQ_OPEN)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReleaseEvidencePackage(Base):
    """版本发布证据包（V3-6 ：证据包完整关联一条链）。

    run×suite×package_version 唯一，再导出 = 版本升级（留旧链）；fingerprint 防重复。
    payload 冗余完整证据 JSON，列级留存点供治理页快速检索：
      代码 code_ref / Prompt prompt_ref / 模型 model_ref / 策略 policy_ref /
      数据集 dataset_checksum+snapshot / Trace trace_ref + 评测 gate 结果 /
      安全 security_ok / 成本 cost_measured / 性能 p95_ms / 人工批准 /
      回滚目标 rollback_target_run_id。
    """

    __tablename__ = "release_evidence_packages"
    __table_args__ = (
        Index("idx_evp_suite", "suite_id", "created_at"),
        Index("idx_evp_run", "run_id"),
        UniqueConstraint("run_id", "suite_id", "package_version",
                         name="uq_evp_run_suite_version"),
        UniqueConstraint("fingerprint", name="uq_evp_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id: Mapped[str] = mapped_column(String(40), nullable=False)
    suite_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("eval_suites.id"), nullable=False
    )
    package_version: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="1.0"
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    code_ref: Mapped[str | None] = mapped_column(String(128))
    prompt_ref: Mapped[str | None] = mapped_column(String(128))
    model_ref: Mapped[str | None] = mapped_column(String(80))
    policy_ref: Mapped[str | None] = mapped_column(String(128))
    dataset_checksum: Mapped[str | None] = mapped_column(String(64))
    dataset_snapshot: Mapped[dict | None] = mapped_column(JSON)
    trace_ref: Mapped[str | None] = mapped_column(String(80))
    gate_id: Mapped[str | None] = mapped_column(String(40))
    decision: Mapped[str | None] = mapped_column(String(40))
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    security_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # None=未测(诚实)
    cost_measured: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    tokens_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    p95_ms: Mapped[float | None] = mapped_column()
    approved_by: Mapped[str | None] = mapped_column(String(36))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    rollback_target_run_id: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
