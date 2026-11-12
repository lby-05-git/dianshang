# -*- coding: utf-8 -*-
"""V3.0 V3-6 高可用与审计：trace_local_queue + release_evidence_packages。

纯 add-only 新表：
- trace_local_queue：Trace 上报失败 -> 本地降级队列（MySQL 落地，不在 Redis——Redis
  正是可能故障的通道）。赔付/决策主流程不落此表（主链路只在旁路 Trace 上报失败时
  才 best-effort 入队），绝不阻塞退款主流程；只存已脱敏 payload，明文 PII 不入库。
- release_evidence_packages：版本发布证据包。一条链完整关联：
  代码 / Prompt / 模型 / 策略 / 数据集 / Trace + 评测/安全/成本/性能 + 人工批准
  与回滚目标。fingerprint = 关联引用集 sha256（幂等防重复导出）。

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trace 本地降级队列：投递键唯一防重复降级；按 status 待消费索引。
    op.create_table(
        "trace_local_queue",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("delivery_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),       # agent_log / stream_append
        sa.Column("payload", sa.JSON(), nullable=False),        # 已脱敏 Trace 记录（明文 PII 永不入库）
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="OPEN"),   # OPEN/DRAINED/DEAD
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(),
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_trace_lq_status", "trace_local_queue", ["status", "enqueued_at"])
    op.create_unique_constraint("uq_trace_lq_delivery", "trace_local_queue", ["delivery_key"])

    # 发布证据包：run×suite×version 唯一（再导出 = 版本升级，幂等留链）。
    op.create_table(
        "release_evidence_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("suite_id", sa.String(36),
                  sa.ForeignKey("eval_suites.id"), nullable=False),
        sa.Column("package_version", sa.String(10), nullable=False,
                  server_default="1.0"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("code_ref", sa.String(128), nullable=True),
        sa.Column("prompt_ref", sa.String(128), nullable=True),
        sa.Column("model_ref", sa.String(80), nullable=True),
        sa.Column("policy_ref", sa.String(128), nullable=True),
        sa.Column("dataset_checksum", sa.String(64), nullable=True),
        sa.Column("dataset_snapshot", sa.JSON(), nullable=True),
        sa.Column("trace_ref", sa.String(80), nullable=True),
        sa.Column("gate_id", sa.String(40), nullable=True),
        sa.Column("decision", sa.String(40), nullable=True),
        sa.Column("passed", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("security_ok", sa.Boolean, nullable=True),   # None=未测（诚实，不假报）
        sa.Column("cost_measured", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("tokens_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("p95_ms", sa.Float, nullable=True),
        sa.Column("approved_by", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rollback_target_run_id", sa.String(40), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),         # 完整证据包（关联引用 + 快照）
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_evp_suite", "release_evidence_packages", ["suite_id", "created_at"])
    op.create_index("idx_evp_run", "release_evidence_packages", ["run_id"])
    op.create_unique_constraint("uq_evp_run_suite_version",
                                "release_evidence_packages",
                                ["run_id", "suite_id", "package_version"])
    op.create_unique_constraint("uq_evp_fingerprint",
                                "release_evidence_packages", ["fingerprint"])


def downgrade() -> None:
    # 直接 drop_table：MySQL 会连同 FK、依赖 FK 的索引、唯一约束一起移除
    # （先显式 drop 索引会在 FK 仍在时被拒：idx_evp_suite 由套件外键所需）。
    op.drop_table("release_evidence_packages")
    op.drop_table("trace_local_queue")