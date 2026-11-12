# -*- coding: utf-8 -*-
"""V2.0 Phase 3 零信任安全强化修复：security_decisions.case_id 放宽为 NULL。

背景：SecurityDecision.case_id 原为 NOT NULL + FK(refund_cases.id)，但
llm_critic / gate_text 的设计（models 注释与调用约定）明确支持
「case_id 为空时以占位审计行留存，上游随后补业务 case」—— 二者矛盾，
llm-critic 独立检查端点（case_id 可空）落库时触发 FK IntegrityError 500。

修复：放宽 case_id 为 NULL（FK 保留；既有行/语义零改动；add-only 原则内的
约束放宽，与 security_events.case_id 可空行为对齐）。

Revision ID: f3e4d5c6b7a9
Revises: f2e3d4c5b6a8
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f3e4d5c6b7a9"
down_revision = "f2e3d4c5b6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "security_decisions", "case_id",
        existing_type=sa.String(36), nullable=True,
    )


def downgrade() -> None:
    # 反向收紧（历史数据中 NULL 行会失败，仅迁移回滚语义）
    op.alter_column(
        "security_decisions", "case_id",
        existing_type=sa.String(36), nullable=False,
    )
