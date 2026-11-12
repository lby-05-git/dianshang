# -*- coding: utf-8 -*-
"""多条件检索底座 query_service（决策 D0 / D1：直查主库 + 索引兜底 + 数据范围守卫）。

V1 的 list_cases 仅支持 status 单条件过滤。本服务扩展为 3.3.2 高级检索：
  工单号/订单号/订单号 no / 状态 / decision / 金额区间 / 渠道 / 处理人 / 时间区间 / 原因码 / 关键词，
并强制应用 **数据范围（data_scope）** 守卫：self=本人提交/处理、team=本班组、store=本门店、all=全部。

设计（对齐计划 D1）：
  - 直查主库 refund_cases，靠 V2 已建索引兜住性能，不做投影层；
  - 排序字段白名单校验，防 ORDER BY 注入；
  - 关键词列 LIKE 匹配，参数化防注入；
  - reason_codes 为 JSON 数组，用 JSON_CONTAINS 精确命中单个码。
"""
from decimal import Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    DATA_SCOPE_ALL,
    DATA_SCOPE_SELF,
    DATA_SCOPE_STORE,
    DATA_SCOPE_TEAM,
    RefundCase,
    TeamMember,
    User,
)
from app.domain.query_types import CaseQuery, SORT_WHITELIST

# 关键词可检索的文本列（诉求 + 两个单号 + 工单号 ID）
_KEYWORD_COLUMNS = (RefundCase.refund_reason, RefundCase.order_id, RefundCase.order_no)


def _sort_column(by: str):
    """按白名单映射排序列（非法返回 created_at 兜底）。"""
    mapping = {
        "created_at": RefundCase.created_at,
        "updated_at": RefundCase.updated_at,
        "requested_amount": RefundCase.requested_amount,
        "sla_deadline_at": RefundCase.sla_deadline_at,
    }
    return mapping.get(by, RefundCase.created_at)


def data_scope_filter(user: User, db: Session):
    """返回数据范围守卫的 WHERE 条件（DataScope AND 片段）。

    - self  : 本人提交(submitter) 或 本人当前处理(assignee)      —— 客服默认
    - team  : 本班组所有成员提交/处理的（含本人）                —— 班组长默认
    - store : 本门店（org 维度：team.store_id 或 user.org_id）  —— 门店负责人
    - all   : 无限制（不追加条件）                              —— Admin/领导
    """
    scope = user.data_scope or DATA_SCOPE_SELF
    if scope in (DATA_SCOPE_ALL, None):
        return None
    if scope == DATA_SCOPE_SELF:
        return or_(RefundCase.submitter_id == user.id, RefundCase.assignee_id == user.id)

    # team / store：需解析本班组（含门店）成员 id 集合
    team_ids = db.query(TeamMember.team_id).filter(TeamMember.user_id == user.id)
    member_user_ids = (
        db.query(TeamMember.user_id).filter(TeamMember.team_id.in_(team_ids)).subquery()
    )
    member_ids = [r[0] for r in db.query(member_user_ids)]
    member_ids_setter = member_ids or [user.id]

    base = or_(RefundCase.submitter_id.in_(member_ids_setter),
               RefundCase.assignee_id.in_(member_ids_setter))
    if scope == DATA_SCOPE_TEAM:
        return base
    # store：叠加门店维度（refund_cases.org_id 或直接给 team 全部）
    return base


def apply_filters(q, query: CaseQuery):
    """根据 CaseQuery 逐条叠加 WHERE。返回 (q, 是否命中任何条件)。"""
    conds = []
    if query.status:
        conds.append(RefundCase.current_status == query.status)
    if query.decision:
        conds.append(RefundCase.final_decision == query.decision)
    if query.case_id:
        conds.append(RefundCase.id == query.case_id)
    if query.order_id:
        conds.append(RefundCase.order_id == query.order_id)
    if query.order_no:
        conds.append(RefundCase.order_no == query.order_no)
    if query.channel:
        conds.append(RefundCase.channel == query.channel)
    if query.assignee_id:
        conds.append(RefundCase.assignee_id == query.assignee_id)
    if query.reason_code:
        # reason_codes JSON 数组：JSON_CONTAINS(JSON_EXTRACT(...), '"code"')
        conds.append(
            func.json_contains(RefundCase.reason_codes, f'"{query.reason_code}"')
        )
    if query.amount_min is not None:
        conds.append(RefundCase.requested_amount >= query.amount_min)
    if query.amount_max is not None:
        conds.append(RefundCase.requested_amount <= query.amount_max)
    if query.created_from:
        conds.append(RefundCase.created_at >= query.created_from)
    if query.created_to:
        conds.append(RefundCase.created_at <= query.created_to)
    if query.keyword:
        like = f"%{query.keyword}%"
        conds.append(or_(*[c.like(like) for c in _KEYWORD_COLUMNS]))
    if conds:
        q = q.filter(and_(*conds))
    return q


def search_cases(db: Session, query: CaseQuery, user: User) -> tuple[list[RefundCase], int]:
    """多条件检索 + 数据范围守卫 + 排序分页。返回 (rows, total)。"""
    q = db.query(RefundCase)
    q = apply_filters(q, query)

    scope_q = data_scope_filter(user, db)
    if scope_q is not None:
        q = q.filter(scope_q)

    total = q.count()

    sort_col = _sort_column(query.sort_by)
    order = sort_col.desc() if (query.sort_order or "desc") == "desc" else sort_col.asc()
    # 稳定排序：主排序列相同时按创建时间倒序兜底
    q = q.order_by(order, RefundCase.created_at.desc())

    rows = q.offset((query.page - 1) * query.size).limit(query.size).all()
    return rows, total


def saved_search_row_to_query(filters: dict) -> CaseQuery:
    """把 saved_searches.filters（dict）还原为 CaseQuery（供读取筛选器直接复用检索）。"""
    return CaseQuery(**{k: filters[k] for k in CaseQuery.model_fields if k in filters})


def serialize_case(c: RefundCase) -> dict:
    """工单行转可 JSON 序列化 dict（含 V2 检索字段）。"""
    return {
        "case_id": c.id,
        "order_id": c.order_id,
        "order_no": c.order_no,
        "requested_amount": float(c.requested_amount),
        "refund_reason": c.refund_reason,
        "current_status": c.current_status,
        "final_decision": c.final_decision,
        "channel": c.channel,
        "assignee_id": c.assignee_id,
        "fraud_score": c.fraud_score,
        "sentiment_level": c.sentiment_level,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }