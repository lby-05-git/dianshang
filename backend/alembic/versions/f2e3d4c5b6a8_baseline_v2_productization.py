# -*- coding: utf-8 -*-
"""基线 V2.0（：产品化、安全强化与多会话）

1) 新增 7 张表：
   - refund_events            ：退款状态独立查询/回执/重试/审计入口（NONE/PENDING/SUCCESS/FAILED 事件流）
   - order_lookup_snapshots   ：订单中心沙箱查询快照（存在性/归属/交易状态/可退资格）
   - security_events          ：安全事件工作台（处置状态 + 审计查询；DLP/注入/越权即录）
   - intent_feedback_samples  ：线上误路由/人工纠正 → 评测候选集（审核后才入 golden）
   - voice_metrics            ：语音会话资源/容量基线（P99/ErrorRate/并发峰值）
   - dead_letter_tasks        ：死信任务（查询/修复/人工重放，可重放不重复退款）
   - dataset_category_map     ：数据集重分类（核心/边界/异常/并发/安全/语音，V2.0 ≥30 条）
2) 既有表最小扩展（add-only）：
   - intent_results  + conflict_candidates / route_source
   - voice_sessions  + reconnects / weak_signal / closed_reason
   - eval_cases      + category
   - eval_runs       + trigger_source
   - refund_cases    + refund_events_guid / last_order_snapshot_id
   - coaching_sessions + difficulty / emotion / scene_name

纯 add-only：不删旧表/不改旧列/不改旧语义；EVAL 隔离、既有索引/约束保持。
Revision ID: f2e3d4c5b6a8
Revises: e2f3a4b5c6d7
Create Date: 2026-08-31
"""
import sqlalchemy as sa
from alembic import op

revision = "f2e3d4c5b6a8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def _create_refund_events() -> None:
    op.create_table(
        "refund_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),      # QUERY/REPLAY/OK/FAIL/RETRY/EXEC
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("new_status", sa.String(16), nullable=False),      # NONE/PENDING/SUCCESS/FAILED
        sa.Column("guid", sa.String(64), nullable=False),            # 事件唯一 GUID（幂等重放）
        sa.Column("gateway_before", sa.String(16), nullable=True),   # 执行前 tool_gate 结果快照
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("guid", name="uq_refund_event_guid"),
    )
    op.create_index("idx_refund_event_case", "refund_events", ["case_id"])


def _create_order_lookup_snapshots() -> None:
    op.create_table(
        "order_lookup_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=False),
        sa.Column("order_id", sa.String(50), nullable=False),
        sa.Column("exists", sa.Boolean(), nullable=False),
        sa.Column("owner_locked", sa.Boolean(), nullable=False),
        sa.Column("trade_status", sa.String(20), nullable=True),     # 交易状态（沙箱/批准服务）
        sa.Column("refundable", sa.Boolean(), nullable=False),
        sa.Column("lookup_kind", sa.String(20), nullable=False, server_default="sandbox"),
        sa.Column("gate_result", sa.String(16), nullable=False, server_default="PASS"),
        sa.Column("reason_codes", sa.JSON(), nullable=True),          # 结构化原因码
        sa.Column("raw_sha256", sa.String(64), nullable=False),       # 快照原文 SHA-256
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_order_lookup_case", "order_lookup_snapshots", ["case_id"])
    op.create_index("idx_order_snap_order", "order_lookup_snapshots", ["order_id"])


def _create_security_events() -> None:
    op.create_table(
        "security_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=True),
        sa.Column("event_type", sa.String(30), nullable=False),      # DLP / INJECTION / UNAUTHORIZED / TOOL_GATE
        sa.Column("severity", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("detail", sa.Text(), nullable=True),                # 掩码后详情
        sa.Column("disposition", sa.String(20), nullable=False, server_default="OPEN"),  # OPEN/TRIAGE/CONFIRMED/FALSE_POSITIVE/CLOSED
        sa.Column("disposed_by", sa.String(36), nullable=True),
        sa.Column("disposed_at", sa.DateTime(), nullable=True),
        sa.Column("audit_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_sec_evt_type", "security_events", ["event_type"])
    op.create_index("idx_sec_evt_case", "security_events", ["case_id"])


def _create_intent_feedback_samples() -> None:
    op.create_table(
        "intent_feedback_samples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=True),
        sa.Column("route_final_intent", sa.String(32), nullable=False),
        sa.Column("human_corrected_intent", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),  # PENDING/REVIEWED/ADOPTED/REJECTED
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_intent_fb_case", "intent_feedback_samples", ["case_id"])


def _create_voice_metrics() -> None:
    op.create_table(
        "voice_metrics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("metric_kind", sa.String(30), nullable=False),      # LATENCY_P99 / ERROR / PEAK_CONCURRENCY / DROP
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_voice_metric_kind", "voice_metrics", ["metric_kind"])


def _create_dead_letter_tasks() -> None:
    op.create_table(
        "dead_letter_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_type", sa.String(30), nullable=False),        # refund / eval / intent
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),  # OPEN/FIXED/REPLAYED/CLOSED
        sa.Column("replayed_ok", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("replay_guid", sa.String(64), nullable=True),        # 重放幂等 GUID
        sa.Column("replayed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_dl_status", "dead_letter_tasks", ["status"])


def _create_dataset_category_map() -> None:
    op.create_table(
        "dataset_category_map",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("suite_id", sa.String(36), sa.ForeignKey("eval_suites.id"), nullable=False),
        sa.Column("case_ref_id", sa.String(20), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),          # core/boundary/anomaly/concurrency/security/voice
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("suite_id", "case_ref_id",
                            name="uq_dataset_cat_suite_case"),
    )
    op.create_index("idx_cat_map_cat", "dataset_category_map", ["category"])


def upgrade() -> None:
    _create_refund_events()
    _create_order_lookup_snapshots()
    _create_security_events()
    _create_intent_feedback_samples()
    _create_voice_metrics()
    _create_dead_letter_tasks()
    _create_dataset_category_map()

    # intent 可靠性（-4）
    op.add_column("intent_results", sa.Column("conflict_candidates", sa.JSON(), nullable=True))
    op.add_column("intent_results", sa.Column("route_source", sa.String(16), nullable=True))

    # 语音产品化（-5）
    op.add_column("voice_sessions", sa.Column("reconnects", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("voice_sessions", sa.Column("weak_signal", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("voice_sessions", sa.Column("closed_reason", sa.String(30), nullable=True))

    # 评测中心（-2）
    op.add_column("eval_cases", sa.Column("category", sa.String(20), nullable=True))
    op.add_column("eval_runs", sa.Column("trigger_source", sa.String(20), nullable=True))

    # 订单/退款产品化（-1）
    op.add_column("refund_cases", sa.Column("refund_events_guid", sa.String(64), nullable=True))
    op.add_column("refund_cases", sa.Column("last_order_snapshot_id", sa.String(36), nullable=True))

    # 陪练产品化（-5）
    op.add_column("coaching_sessions", sa.Column("difficulty", sa.String(20), nullable=True))
    op.add_column("coaching_sessions", sa.Column("emotion", sa.String(20), nullable=True))
    op.add_column("coaching_sessions", sa.Column("scene_name", sa.String(64), nullable=True))


def downgrade() -> None:
    for col in ("scene_name", "emotion", "difficulty"):
        op.drop_column("coaching_sessions", col)
    op.drop_column("refund_cases", "last_order_snapshot_id")
    op.drop_column("refund_cases", "refund_events_guid")
    op.drop_column("eval_runs", "trigger_source")
    op.drop_column("eval_cases", "category")
    op.drop_column("voice_sessions", "closed_reason")
    op.drop_column("voice_sessions", "weak_signal")
    op.drop_column("voice_sessions", "reconnects")
    op.drop_column("intent_results", "route_source")
    op.drop_column("intent_results", "conflict_candidates")
    op.drop_table("dataset_category_map")
    op.drop_table("dead_letter_tasks")
    op.drop_table("voice_metrics")
    op.drop_table("intent_feedback_samples")
    op.drop_table("security_events")
    op.drop_table("order_lookup_snapshots")
    op.drop_table("refund_events")