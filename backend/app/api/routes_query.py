# -*- coding: utf-8 -*-
"""检索路由（3.3.2 高级检索）：多条件搜索 + 筛选器保存/读取 + 导出。

依赖 query_service 底座：
  - GET /api/v1/search/cases   —— 多条件检索（含数据范围守卫）
  - GET/POST/DELETE /api/v1/search/saved   —— 保存/列出/删除筛选器
  - GET /api/v1/search/export  —— CSV 导出（LEADER+ 且记审计）
"""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import ROLE_ADMIN, ROLE_LEADER, get_current_user, require_roles
from app.api.schemas import SavedSearchCreate, SavedSearchItem, SearchResponse, SearchResultItem
from app.domain.models import SavedSearch, User
from app.domain.query_types import CaseQuery
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.services.query_service import search_cases, serialize_case

router = APIRouter(prefix="/search", tags=["search"])


def _to_item(r) -> SearchResultItem:
    from app.api.schemas import SearchResultItem
    return SearchResultItem(**serialize_case(r))


@router.get("/cases", response_model=SearchResponse)
def search(qu: CaseQuery = Depends(), user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    rows, total = search_cases(db, qu, user)
    return SearchResponse(total=total, items=[_to_item(r) for r in rows])


@router.get("/saved", response_model=list[SavedSearchItem])
def list_saved(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(SavedSearch).filter_by(user_id=user.id)
            .order_by(SavedSearch.created_at.desc()).all())
    return [SavedSearchItem(id=r.id, name=r.name, filters=r.filters) for r in rows]


@router.post("/saved", status_code=201, response_model=SavedSearchItem)
def create_saved(body: SavedSearchCreate, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    row = SavedSearch(user_id=user.id, name=body.name, filters=body.filters)
    db.add(row)
    db.commit()
    db.refresh(row)
    return SavedSearchItem(id=row.id, name=row.name, filters=row.filters)


@router.delete("/saved/{filter_id}")
def delete_saved(filter_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    row = db.query(SavedSearch).filter_by(id=filter_id, user_id=user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="筛选器不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/export")
def export_csv(qu: CaseQuery = Depends(),
               leader: User = Depends(require_roles(ROLE_LEADER, ROLE_ADMIN)),
               db: Session = Depends(get_db)):
    """仅 LEADER+/ADMIN 可导出；记审计事件（3.2.2 导出管控）。"""
    rows, _ = search_cases(db, qu, leader)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["case_id", "order_id", "order_no", "amount", "status", "decision",
                "channel", "assignee_id", "created_at"])
    for r in rows:
        w.writerow([r.id, r.order_id, r.order_no, r.requested_amount,
                    r.current_status, r.final_decision, r.channel,
                    r.assignee_id, r.created_at])
    write_event(db=db, actor_id=leader.id, action="DATA_EXPORTED",
                payload={"rows": len(rows)})
    db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="cases.csv"'})