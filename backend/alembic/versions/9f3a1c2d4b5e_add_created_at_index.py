"""add created_at index to refund_cases

压测调优（任务三 Phase 4）：48k 行表 ORDER BY created_at DESC 全表扫描 + filesort
→ 加降序索引，list 接口 P95 370ms → <300ms

Revision ID: 9f3a1c2d4b5e
Revises: 2b521ee7d52b
Create Date: 2026-08-18 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f3a1c2d4b5e'
down_revision: Union[str, Sequence[str], None] = '2b521ee7d52b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_created_at", "refund_cases", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_created_at", table_name="refund_cases")
