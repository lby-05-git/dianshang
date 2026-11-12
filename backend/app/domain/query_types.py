# -*- coding: utf-8 -*-
"""查询基础类型：检索条件与筛选器（3.3.2 高级检索）。"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.domain.models import (
    DATA_SCOPE_SELF,
    DATA_SCOPE_TEAM,
    DATA_SCOPE_STORE,
    DATA_SCOPE_ALL,
)


# ---- 数据范围常量（复用 models 中的角色/范围语义）----
DATA_SCOPE_CHOICES = (DATA_SCOPE_SELF, DATA_SCOPE_TEAM, DATA_SCOPE_STORE, DATA_SCOPE_ALL)


class CaseQuery(BaseModel):
    """多条件检索条件（3.3.2）。所有条件均可空；全空 = 全量倒序分页。

    - 关键词：匹配 refund_reason / order_id / order_no（诉求/单号）。
    - 金额区间/时间区间：含边界。
    - 原因码：JSON 数组任一命中（reason_codes @> :code）。
    """
    status: str | None = None                       # current_status
    decision: str | None = None                     # final_decision
    order_id: str | None = None
    order_no: str | None = None
    case_id: str | None = None
    channel: str | None = None
    assignee_id: str | None = None
    reason_code: str | None = None                  # reason_codes JSON 任一命中
    keyword: str | None = None                      # 诉求/OCR/单号全文
    amount_min: Decimal | None = None
    amount_max: Decimal | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)
    sort_by: str = "created_at"                     # created_at / requested_amount / sla_deadline_at
    sort_order: str = "desc"                        # asc / desc


# 允许排序的字段白名单（防 SQL 注入：绝不拼接任意字符串进 ORDER BY）
SORT_WHITELIST = ("created_at", "requested_amount", "sla_deadline_at", "updated_at")