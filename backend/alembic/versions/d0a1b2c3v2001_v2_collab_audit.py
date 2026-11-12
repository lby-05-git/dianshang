"""V2 collab & audit: 新增 9 表 + 既有表加列 + 索引 + 默认策略档

任务一（V2 数据模型与迁移）：
1. users 加 org_id / team_id / data_scope
2. refund_cases 加 order_no / org_id / channel / reason_codes / claimed_at / sla_deadline_at / refund_status / trace_id
3. agent_execution_logs 加 trace_id
4. 新增 9 张表：teams / team_members / policy_profiles / policy_bindings /
   complaint_notes / domain_events / refund_transactions / saved_searches / kpi_daily_rollups
5. refund_cases 新增检索/看板复合索引
6. 预置默认策略档（阈值 = MVP）

Revision ID: d0a1b2c3v2001
Revises: 4a1b2c3d4e5f
Create Date: 2026-08-23 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0a1b2c3v2001'
down_revision: Union[str, Sequence[str], None] = '4a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # ============ 既有表加列（全部可空/给默认，不破坏 V1 语义） ============
    # ---- users：data_scope 默认 'self' ----
    op.add_column('users', sa.Column('org_id', sa.String(length=36), nullable=True))
    op.add_column('users', sa.Column('team_id', sa.String(length=36), nullable=True))
    op.add_column('users', sa.Column('data_scope', sa.String(length=20), nullable=False, server_default='self'))

    # ---- refund_cases ----
    op.add_column('refund_cases', sa.Column('order_no', sa.String(length=64), nullable=True))
    op.add_column('refund_cases', sa.Column('org_id', sa.String(length=36), nullable=True))
    op.add_column('refund_cases', sa.Column('channel', sa.String(length=20), nullable=False, server_default='APP'))
    op.add_column('refund_cases', sa.Column('reason_codes', sa.JSON(), nullable=True))
    op.add_column('refund_cases', sa.Column('claimed_at', sa.DateTime(), nullable=True))
    op.add_column('refund_cases', sa.Column('sla_deadline_at', sa.DateTime(), nullable=True))
    op.add_column('refund_cases', sa.Column('refund_status', sa.String(length=20), nullable=False, server_default='NONE'))
    op.add_column('refund_cases', sa.Column('trace_id', sa.String(length=64), nullable=True))

    # ---- agent_execution_logs ----
    op.add_column('agent_execution_logs', sa.Column('trace_id', sa.String(length=64), nullable=True))

    # ============ 2. 新增复合索引（支撑检索/看板） ============
    op.create_index('idx_status_sla', 'refund_cases', ['current_status', 'sla_deadline_at'], unique=False)
    op.create_index('idx_assignee_status', 'refund_cases', ['assignee_id', 'current_status', 'updated_at'], unique=False)
    op.create_index('idx_org_created', 'refund_cases', ['org_id', 'created_at'], unique=False)
    op.create_index('idx_order_no', 'refund_cases', ['order_no'], unique=False)

    # ============ 3. 新增 9 张表 ============

    # ---- teams / team_members ----
    op.create_table('teams',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('store_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('team_members',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('team_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_team_member_user', 'team_members', ['user_id'], unique=False)

    # ---- policy_profiles / policy_bindings ----
    op.create_table('policy_profiles',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('amount_threshold', sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column('ocr_confidence', sa.Integer(), nullable=False),
        sa.Column('fraud_pass', sa.Integer(), nullable=False),
        sa.Column('fraud_suspend', sa.Integer(), nullable=False),
        sa.Column('blacklist_autoreject', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('sla_remind_hr', sa.Integer(), nullable=False),
        sa.Column('sla_escalate_hr', sa.Integer(), nullable=False),
        sa.Column('is_default', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('policy_bindings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('profile_id', sa.String(length=36), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['policy_profiles.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_policy_binding_profile', 'policy_bindings', ['profile_id'], unique=False)

    # ---- complaint_notes 内部批注 ----
    op.create_table('complaint_notes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=False),
        sa.Column('author_id', sa.String(length=36), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('mentioned_user_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['refund_cases.id']),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )

    # ---- domain_events 领域事件（append-only 审计） ----
    op.create_table('domain_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=True),
        sa.Column('actor_id', sa.String(length=36), nullable=True),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_domain_event_actor', 'domain_events', ['actor_id'], unique=False)
    op.create_index('idx_domain_event_case', 'domain_events', ['case_id'], unique=False)

    # ---- refund_transactions 退款请求与回执 ----
    op.create_table('refund_transactions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('case_id', sa.String(length=36), nullable=False),
        sa.Column('refund_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.DECIMAL(precision=10, scale=2), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['refund_cases.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_refund_case', 'refund_transactions', ['case_id'], unique=False)

    # ---- saved_searches 用户筛选器 ----
    op.create_table('saved_searches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('filters', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )

    # ---- kpi_daily_rollups 看板日汇总 ----
    op.create_table('kpi_daily_rollups',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('rollup_date', sa.String(length=20), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=False),
        sa.Column('total_cases', sa.Integer(), nullable=False),
        sa.Column('auto_approve_rate', sa.Float(), nullable=False),
        sa.Column('suspend_rate', sa.Float(), nullable=False),
        sa.Column('reject_rate', sa.Float(), nullable=False),
        sa.Column('human_p50_ms', sa.Integer(), nullable=True),
        sa.Column('human_p95_ms', sa.Integer(), nullable=True),
        sa.Column('completed_per_agent', sa.Float(), nullable=False),
        sa.Column('agent_fallback_rate', sa.Float(), nullable=False),
        sa.Column('ocr_low_conf_rate', sa.Float(), nullable=False),
        sa.Column('sla_breach_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # ============ 4. 预置默认策略档（MVP 阈值）= 300 / 60 / 20 / 是 / 4h / 24h ============
    result = bind.execute(
        sa.text("SELECT id FROM policy_profiles WHERE is_default = 1 LIMIT 1")
    ).fetchone()
    if result is None:
        bind.execute(
            sa.text("""
                INSERT INTO policy_profiles
                    (id, name, amount_threshold, ocr_confidence, fraud_pass, fraud_suspend,
                     blacklist_autoreject, sla_remind_hr, sla_escalate_hr, is_default)
                VALUES
                    ('default-mvp', '默认档-MVP', 300.00, 60, 20, 60, 1, 4, 24, 1)
            """)
        )
        # 默认档绑定三个渠道
        for ch in ('APP', '小程序', '客服代提'):
            bind.execute(
                sa.text("""
                    INSERT INTO policy_bindings (id, profile_id, channel)
                    VALUES (UUID(), 'default-mvp', :ch)
                """),
                {"ch": ch},
            )


def downgrade() -> None:
    """回滚（幂等容错：缺失的表/列/索引跳过，不破坏 V1 六表）。

    MySQL 约束：外键会占用子表索引，导致「先删索引」报 1553。
    因此有外键的子表一律**先删表**（连带释放外键与索引），
    再单独删主表 refund_cases 上无外键占用的检索索引。
    """
    bind = op.get_bind()

    def table_exists(name: str) -> bool:
        return (
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                ),
                {"t": name},
            ).scalar()
            or 0
        )

    def col_exists(tbl: str, col: str) -> bool:
        return (
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                    "AND COLUMN_NAME = :c"
                ),
                {"t": tbl, "c": col},
            ).scalar()
            or 0
        )

    def idx_exists(tbl: str, idx: str) -> bool:
        return (
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM information_schema.STATISTICS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                    "AND INDEX_NAME = :i"
                ),
                {"t": tbl, "i": idx},
            ).scalar()
            or 0
        )

    # ---- 1. 删新增的子表（按外键依赖由内到外；存在才删，连带释放外键+索引）----
    for tbl in [
        "kpi_daily_rollups",
        "saved_searches",            # FK -> users
        "refund_transactions",       # FK -> refund_cases；自带 idx_refund_case 随表删除
        "policy_bindings",           # FK -> policy_profiles；自带 idx_policy_binding_profile 随表删除
        "complaint_notes",           # FK -> refund_cases + users
        "domain_events",             # 无 FK，但含索引随表删除
        "policy_profiles",
        "team_members",              # FK -> teams + users；自带 idx_team_member_user 随表删除
        "teams",
    ]:
        if table_exists(tbl):
            op.drop_table(tbl)

    # ---- 2. 删 refund_cases 新增检索索引（主表自身索引，无外键占用）----
    for idx in [
        "idx_status_sla",
        "idx_assignee_status",
        "idx_org_created",
        "idx_order_no",
    ]:
        if idx_exists("refund_cases", idx):
            op.drop_index(idx, table_name="refund_cases")

    # ---- 3. 删既有表加列（存在才删）----
    add_col_drops = [
        ("agent_execution_logs", "trace_id"),
        ("refund_cases", "trace_id"),
        ("refund_cases", "refund_status"),
        ("refund_cases", "sla_deadline_at"),
        ("refund_cases", "claimed_at"),
        ("refund_cases", "reason_codes"),
        ("refund_cases", "channel"),
        ("refund_cases", "org_id"),
        ("refund_cases", "order_no"),
        ("users", "data_scope"),
        ("users", "team_id"),
        ("users", "org_id"),
    ]
    for tbl, col in add_col_drops:
        if col_exists(tbl, col):
            op.drop_column(tbl, col)