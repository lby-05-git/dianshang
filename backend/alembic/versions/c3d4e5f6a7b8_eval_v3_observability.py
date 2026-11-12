# -*- coding: utf-8 -*-
"""M20 漂移监测 + 成本预算告警：eval_budgets / eval_drift_indicators / eval_alerts

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29
"""
import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_budgets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dimension", sa.String(length=30), nullable=False),
        sa.Column("dim_key", sa.String(length=80), nullable=False),
        sa.Column("period", sa.String(length=10), nullable=False),
        sa.Column("limit_amount", sa.Float(), server_default="100.0", nullable=False),
        sa.Column("limit_tokens", sa.Integer(), server_default="1000000", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dimension", "dim_key", "period",
                            name="uq_eval_budget_dim_period"),
    )
    op.create_index("idx_eval_budget_dim", "eval_budgets", ["dimension", "dim_key"],
                    unique=False)

    op.create_table(
        "eval_drift_indicators",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("suite_id", sa.String(length=36), nullable=True),
        sa.Column("metric_key", sa.String(length=40), nullable=False),
        sa.Column("value", sa.Float(), server_default="0", nullable=False),
        sa.Column("baseline", sa.Float(), server_default="0", nullable=False),
        sa.Column("threshold", sa.Float(), server_default="0", nullable=False),
        sa.Column("drifted", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_drift_run", "eval_drift_indicators", ["run_id"],
                    unique=False)

    op.create_table(
        "eval_alerts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_type", sa.String(length=30), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("suite_id", sa.String(length=36), nullable=True),
        sa.Column("metric_key", sa.String(length=40), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("severity", sa.String(length=10), server_default="warning", nullable=False),
        sa.Column("acked", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("acked_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_alert_run", "eval_alerts", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_eval_alert_run", table_name="eval_alerts")
    op.drop_table("eval_alerts")
    op.drop_index("idx_eval_drift_run", table_name="eval_drift_indicators")
    op.drop_table("eval_drift_indicators")
    op.drop_index("idx_eval_budget_dim", table_name="eval_budgets")
    op.drop_table("eval_budgets")