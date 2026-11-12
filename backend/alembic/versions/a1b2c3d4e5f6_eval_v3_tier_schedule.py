# -*- coding: utf-8 -*-
"""评测中心 V3.0：数据集分级 + 调度（持续评测治理）。

迁移链：f1a2b3c4d5e6 (V2 评测中心 6 表) -> a1b2c3d4e5f6 (V3 分级调度)。
只新增列/表，不改既有表结构；downgrade 按依赖顺序（子→父）删除。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 数据集分级：{tier: [case ids]} （smoke ⊂ core ⊂ full）
    op.add_column("eval_suites", sa.Column("tier_map", sa.JSON(), nullable=True))

    # ---- eval_schedules 评测调度 ----
    op.create_table(
        "eval_schedules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suite_id", sa.String(length=36), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("cadence", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.String(length=40), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_schedule_suite", "eval_schedules", ["suite_id"], unique=False)
    op.create_unique_constraint(
        "uq_eval_schedule_suite_tier", "eval_schedules", ["suite_id", "tier"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_eval_schedule_suite_tier", "eval_schedules", type_="unique")
    op.drop_index("idx_eval_schedule_suite", table_name="eval_schedules")
    op.drop_table("eval_schedules")

    op.drop_column("eval_suites", "tier_map")