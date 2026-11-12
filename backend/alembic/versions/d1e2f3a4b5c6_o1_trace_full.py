# -*- coding: utf-8 -*-
"""Phase O：贯穿 Trace 结构（~O07）

- O01/O04：agent_execution_logs 补 `trace_id` 从未写入 -> 本次只保证模型/写入侧，
  历史行不受影响；新增 `trace` JSON 列承载 Tool Trace 结构（tools 数组）。
- O07 向后兼容：纯加列，不删旧表/不改旧列。

Revision ID: d1e2f3a4b5c6
Revises: c3d4e5f6a7b8
Create Date: 2026-08-30
"""
import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_execution_logs",
        sa.Column("trace", sa.JSON(), nullable=True),
    )
    op.add_column(
        "agent_execution_logs",
        sa.Column("model", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_execution_logs", "model")
    op.drop_column("agent_execution_logs", "trace")