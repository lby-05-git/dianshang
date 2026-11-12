# -*- coding: utf-8 -*-
"""V3.0 V3-4 意图模型治理：intent_classifier_versions + intent_replay_queue。

纯 add-only 新表：
- intent_classifier_versions：规则/词典/轻量模型/LLM 分类器各自独立版本导出
  （内容 sha256 指纹 + 批准留痕）。只读注册表，绝不自动改线上路由行为。
- intent_replay_queue：feedback ADOPTED → 待审核回放集（去重/限长/审核流转）。
  绝不自动改生产规则；APPROVED 只是「列入回放验证候选」，需人工看板确认后才进
  黄金集（既有的 golden 冻结流程，不做任何静默改写）。

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 分类器版本注册：classifier_name=rule/dict/lightweight/llm；(name, version) 唯一。
    op.create_table(
        "intent_classifier_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("classifier_name", sa.String(24), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="DRAFT"),    # DRAFT/APPROVED/REJECTED
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),     # 快照：规则集/词典/模型指纹/提示词
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(36), nullable=True),
        sa.Column("approver_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_intent_cls_name", "intent_classifier_versions", ["classifier_name"])
    op.create_unique_constraint("uq_intent_cls_name_version",
                                "intent_classifier_versions", ["classifier_name", "version"])

    # 意图回放集：每条 ADOPTED 反馈至多一行（sample_id 唯一防重复入队）。
    op.create_table(
        "intent_replay_queue",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sample_id", sa.String(36),
                  sa.ForeignKey("intent_feedback_samples.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="PENDING"),  # PENDING/APPROVED/SKIPPED
        sa.Column("reviewed_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_intent_replay_status", "intent_replay_queue", ["status"])
    op.create_unique_constraint("uq_intent_replay_sample",
                                "intent_replay_queue", ["sample_id"])


def downgrade() -> None:
    op.drop_constraint("uq_intent_replay_sample", "intent_replay_queue", type_="unique")
    op.drop_index("idx_intent_replay_status", table_name="intent_replay_queue")
    op.drop_table("intent_replay_queue")
    op.drop_constraint("uq_intent_cls_name_version", "intent_classifier_versions", type_="unique")
    op.drop_index("idx_intent_cls_name", table_name="intent_classifier_versions")
    op.drop_table("intent_classifier_versions")