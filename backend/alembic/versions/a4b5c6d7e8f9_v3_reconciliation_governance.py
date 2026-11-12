# -*- coding: utf-8 -*-
"""V3.0 V3-1 订单/退款一致性治理：reconciliation_events + order_source_verdicts。

纯 add-only 新表（不动既有表语义；对账只写审计，绝不改退款/决策状态）。
Revision ID: a4b5c6d7e8f9
Revises: f3e4d5c6b7a9
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a4b5c6d7e8f9"
down_revision = "f3e4d5c6b7a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"),
                  nullable=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("field", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="INFO"),
        sa.Column("expected", sa.JSON(), nullable=True),
        sa.Column("actual", sa.JSON(), nullable=True),
        sa.Column("diff_detail", sa.Text(), nullable=True),
        sa.Column("guid", sa.String(64), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_recon_case", "reconciliation_events", ["case_id"])
    op.create_unique_constraint("uq_recon_event_guid", "reconciliation_events", ["guid"])

    op.create_table(
        "order_source_verdicts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"),
                  nullable=False),
        sa.Column("order_id", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(30), nullable=False),
        sa.Column("trusted", sa.Boolean(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("reason", sa.JSON(), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_osv_case", "order_source_verdicts", ["case_id"])


def downgrade() -> None:
    op.drop_index("idx_osv_case", table_name="order_source_verdicts")
    op.drop_table("order_source_verdicts")
    op.drop_constraint("uq_recon_event_guid", "reconciliation_events", type_="unique")
    op.drop_index("idx_recon_case", table_name="reconciliation_events")
    op.drop_table("reconciliation_events")