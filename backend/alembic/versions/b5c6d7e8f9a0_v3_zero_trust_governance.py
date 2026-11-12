# -*- coding: utf-8 -*-
"""V3.0 V3-3 企业级零信任治理：tool_definitions + version_registry。

纯 add-only 新表（不动既有表语义；版本中心只留痕，安全分级只读不擅自改工具行为——
enabled=False 的 fund_effect 工具在检查层直接拒绝，绝不绕过既有退款门禁）。
Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 工具风险分级注册表：tool_name 唯一；policy_id 绑定已批准策略档。
    # risk_class 取值 readonly / internal_write / external_notify / fund_effect
    # （V3-3 四级风险；fund_effect 退款类要求 策略+HITL+幂等+审计 四同时）。
    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("risk_class", sa.String(24), nullable=False),
        sa.Column("description", sa.String(120), nullable=True),
        sa.Column("policy_id", sa.String(36),
                  sa.ForeignKey("policy_profiles.id"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("requires_hitl", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("requires_idempotency", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("requires_audit", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(),
                  server_default=sa.text("CURRENT_TIMESTAMP"),
                  onupdate=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_unique_constraint("uq_tool_definitions_name", "tool_definitions", ["tool_name"])

    # 版本注册中心：Agent/Prompt/模型/工具/权限/数据集/安全策略 统一版本管理。
    # (entity_type, entity_name, version) 唯一防重；approver/rollback 留痕（只增不改）。
    op.create_table(
        "version_registry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="DRAFT"),    # DRAFT/CANDIDATE/APPROVED/REJECTED/ROLLED_BACK
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("creator_id", sa.String(36), nullable=True),
        sa.Column("approver_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rollback_to", sa.String(40), nullable=True),   # 回滚基准版本号
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_ver_reg_entity", "version_registry", ["entity_type", "entity_name"])
    op.create_unique_constraint("uq_ver_reg_entity_version",
                                "version_registry", ["entity_type", "entity_name", "version"])


def downgrade() -> None:
    op.drop_constraint("uq_ver_reg_entity_version", "version_registry", type_="unique")
    op.drop_index("idx_ver_reg_entity", table_name="version_registry")
    op.drop_table("version_registry")
    op.drop_constraint("uq_tool_definitions_name", "tool_definitions", type_="unique")
    op.drop_table("tool_definitions")