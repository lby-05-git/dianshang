"""V1.0 评测：refund_cases 订单/商品/价格核验列 + 评测关联列

：订单真实性、商品一致性、价格一致性必须与工单主表
分立管理并对评测可见。本迁移为纯增量列（全部允许 NULL/默认值，向后兼容），
不修改任何既有列、约束或数据；downgrade 只删新增列，可完整回滚。

Revision ID: e1f2a3b4c5d6
Revises: d0a1b2c3v2001
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0a1b2c3v2001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refund_cases", sa.Column("order_verification", sa.String(20), nullable=True))
    op.add_column("refund_cases", sa.Column("order_verification_reasons", sa.JSON(), nullable=True))
    op.add_column("refund_cases", sa.Column("product_verification", sa.String(20), nullable=True))
    op.add_column("refund_cases", sa.Column("product_verification_reasons", sa.JSON(), nullable=True))
    op.add_column("refund_cases", sa.Column("price_verification", sa.String(20), nullable=True))
    op.add_column("refund_cases", sa.Column("price_verification_reasons", sa.JSON(), nullable=True))
    op.add_column("refund_cases", sa.Column("eval_run_id", sa.String(64), nullable=True))
    op.add_column("refund_cases", sa.Column("eval_case_id", sa.String(64), nullable=True))
    op.create_index("idx_refund_cases_eval_run", "refund_cases", ["eval_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_refund_cases_eval_run", table_name="refund_cases")
    op.drop_column("refund_cases", "eval_case_id")
    op.drop_column("refund_cases", "eval_run_id")
    op.drop_column("refund_cases", "price_verification_reasons")
    op.drop_column("refund_cases", "price_verification")
    op.drop_column("refund_cases", "product_verification_reasons")
    op.drop_column("refund_cases", "product_verification")
    op.drop_column("refund_cases", "order_verification_reasons")
    op.drop_column("refund_cases", "order_verification")