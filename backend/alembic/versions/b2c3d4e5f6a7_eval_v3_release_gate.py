# -*- coding: utf-8 -*-
"""M19 发布门禁：eval_baselines + eval_gate_results

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-29
"""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_baselines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suite_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("dataset_checksum", sa.String(length=64), nullable=True),
        sa.Column("suite_version", sa.String(length=20), nullable=True),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suite_id", "role", name="uq_eval_baseline_suite_role"),
    )
    op.create_index("idx_eval_baseline_suite_role", "eval_baselines",
                    ["suite_id", "role"], unique=False)

    op.create_table(
        "eval_gate_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("gate_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("baseline_run_id", sa.String(length=40), nullable=True),
        sa.Column("suite_id", sa.String(length=36), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column(
            "passed", sa.Boolean(), server_default=sa.text("0"), nullable=False,
        ),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_gate_run", "eval_gate_results", ["run_id"],
                    unique=False)


def downgrade() -> None:
    op.drop_index("idx_eval_gate_run", table_name="eval_gate_results")
    op.drop_table("eval_gate_results")
    op.drop_index("idx_eval_baseline_suite_role", table_name="eval_baselines")
    op.drop_table("eval_baselines")