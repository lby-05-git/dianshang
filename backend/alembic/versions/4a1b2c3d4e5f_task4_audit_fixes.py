"""task4 audit fixes: unique intervention + evidences md5 index

任务四审计修复（docs/AI生成代码安全与性能审计报告）：
1. human_interventions 加 UNIQUE(case_id)：Redis 审批锁 TTL 过期窗口内的
   并发双审批最后一道防线（DB 层兜底，防资损）
2. evidences 加 idx_evidences_md5：fraud 节点凭证复用检测（file_md5 等值查询）
   在压测 6 万+ 凭证后全表扫描，加索引后走 BTREE

Revision ID: 4a1b2c3d4e5f
Revises: 9f3a1c2d4b5e
Create Date: 2026-08-18 19:50:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4a1b2c3d4e5f'
down_revision: Union[str, Sequence[str], None] = '9f3a1c2d4b5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint("uq_human_interventions_case_id", "human_interventions", ["case_id"])
    op.create_index("idx_evidences_md5", "evidences", ["file_md5"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_evidences_md5", table_name="evidences")
    op.drop_constraint("uq_human_interventions_case_id", "human_interventions", type_="unique")
