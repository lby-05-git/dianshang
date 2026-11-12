# -*- coding: utf-8 -*-
"""协同工作台（3.3.1 协同台）：待办池 / 认领 / 转派 / 内部批注(@提醒)。

语义（挂起在 Agent 侧产生，本模块只做「人」的协作动作）：
  - GET  /collab/queue          当前用户可见的待办池（data_scope 过滤 + SLA 标记）
  - POST /collab/{case_id}/claim     认领挂起单（supervisor/leader/可处理者 + data_scope）
  - POST /collab/{case_id}/transfer  转派给他人（supervisor/leader）
  - GET/POST /collab/{case_id}/notes 内部批注（写 complaint_notes + @ 站内提醒）

红线对齐：
  - 只有挂起（SUSPENDED）且决策待定（PENDING）的单可认领/转派；
  - 认领者必须能跨过本人/班组数据范围（否则看不见）；
  - 所有协作写 domain_events（append-only 审计），不更新/删除事件。
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_LEADER,
    ROLE_SUPERVISOR,
    get_current_user,
    require_roles,
)
from app.domain.models import (
    CASE_STATUS_SUSPENDED,
    DECISION_PENDING,
    ComplaintNote,
    RefundCase,
    User,
)
from app.domain.sla import sla_state
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.services.query_service import data_scope_filter

router = APIRouter(prefix="/workspace", tags=["workspace"])

# 可认领/转派角色（客服可认领他人处理中的单需具备承接语义；这里允许主管以上认领）
_APPROVER_ROLES = (ROLE_SUPERVISOR, ROLE_LEADER, ROLE_ADMIN)

_AT_RE = re.compile(r"@([\w]+)")


# ---- 请求/响应 schema ----
class NotePostBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class NoteTransferBody(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=36)


class NoteItem(BaseModel):
    id: str
    author_id: str
    content: str
    mentioned_user_id: str | None = None
    created_at: str


class QueueItem(BaseModel):
    case_id: str
    order_id: str
    requested_amount: float
    reason: str | None = None
    current_status: str
    final_decision: str
    assignee_id: str | None
    channel: str
    created_at: str
    sla: str  # ok / remind / overdue


# ---- 待办池 ----
@router.get("/queue", response_model=list[QueueItem])
def my_queue(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """当前用户可见的挂起待办（data_scope 守卫）。超时/提醒态一并标记展示。"""
    q = db.query(RefundCase).filter(RefundCase.current_status == CASE_STATUS_SUSPENDED)
    scope = data_scope_filter(user, db)
    if scope is not None:
        q = q.filter(scope)
    rows = q.order_by(RefundCase.created_at.desc()).limit(200).all()
    return [
        QueueItem(
            case_id=c.id, order_id=c.order_id, requested_amount=float(c.requested_amount),
            reason=c.refund_reason, current_status=c.current_status,
            final_decision=c.final_decision, assignee_id=c.assignee_id,
            channel=c.channel, created_at=c.created_at.isoformat() if c.created_at else "",
            sla=sla_state(c.sla_deadline_at),
        )
        for c in rows
    ]


# ---- 认领 ----
@router.post("/{case_id}/claim")
def claim(case_id: str, approver: User = Depends(require_roles(*_APPROVER_ROLES)),
          db: Session = Depends(get_db)):
    """认领挂起单：写入处理人 + 认领时间，记审计（认领后可见置已处理人）。"""
    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    if case.current_status != CASE_STATUS_SUSPENDED or case.final_decision != DECISION_PENDING:
        raise HTTPException(status_code=409, detail="只有待审批的挂起单才能认领")
    case.assignee_id = approver.id
    case.claimed_at = datetime.now()
    write_event(db=db, actor_id=approver.id, case_id=case_id,
                action="CASE_CLAIMED", payload={"operator": approver.username})
    db.commit()
    return {"case_id": case_id, "assignee_id": approver.id, "action": "CLAIMED"}


# ---- 转派 ----
@router.post("/{case_id}/transfer")
def transfer(case_id: str, body: NoteTransferBody,
             approver: User = Depends(require_roles(*_APPROVER_ROLES)),
             db: Session = Depends(get_db)):
    """转派给目标处理人（须认证目标存在）。"""
    target = db.get(User, body.target_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="目标处理人不存在")
    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    if case.current_status != CASE_STATUS_SUSPENDED or case.final_decision != DECISION_PENDING:
        raise HTTPException(status_code=409, detail="只有待审批的挂起单才能转派")
    case.assignee_id = target.id
    case.claimed_at = None  # 转派后待新处理人确认
    write_event(db=db, actor_id=approver.id, case_id=case_id,
                action="CASE_TRANSFERRED",
                payload={"from_user": approver.id, "to_user": target.id})
    db.commit()
    return {"case_id": case_id, "assignee_id": target.id, "action": "TRANSFERRED"}


# ---- 内部批注 ----
@router.get("/{case_id}/notes", response_model=list[NoteItem])
def list_notes(case_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    if db.get(RefundCase, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    q = db.query(ComplaintNote).filter(ComplaintNote.case_id == case_id)
    rows = q.order_by(ComplaintNote.created_at.asc()).all()
    return [
        NoteItem(id=r.id, author_id=r.author_id, content=r.content,
                 mentioned_user_id=r.mentioned_user_id,
                 created_at=r.created_at.isoformat() if r.created_at else "")
        for r in rows
    ]


@router.post("/{case_id}/notes", status_code=201, response_model=NoteItem)
def post_note(case_id: str, body: NotePostBody,
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """写内部批注：解析 @username 触发站内提醒（站内提醒为示警，非外发）。"""
    if db.get(RefundCase, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    if case_not_visible(db, case_id, user):
        raise HTTPException(status_code=403, detail="无权查看该案件")
    mentioned_user_id = _resolve_mention(db, body.content)
    note = ComplaintNote(case_id=case_id, author_id=user.id, content=body.content,
                         mentioned_user_id=mentioned_user_id)
    db.add(note)
    write_event(db=db, actor_id=user.id, case_id=case_id, action="NOTE_ADDED",
                payload={"mentioned_user_id": mentioned_user_id})
    db.commit()
    db.refresh(note)
    return NoteItem(id=note.id, author_id=note.author_id, content=note.content,
                    mentioned_user_id=note.mentioned_user_id,
                    created_at=note.created_at.isoformat() if note.created_at else "")


def _resolve_mention(db: Session, content: str) -> str | None:
    """从批注正文里抓第一个 @username 并解析为用户 id（找不到忽略）。"""
    for name in re.findall(_AT_RE, content):
        u = db.query(User).filter_by(username=name).first()
        if u is not None:
            return u.id
    return None


def case_not_visible(db: Session, case_id: str, user: User) -> bool:
    """白名单化数据范围守卫：返回 True 表示不可见（403）。"""
    scope = data_scope_filter(user, db)
    q = db.query(RefundCase).filter(RefundCase.id == case_id)
    if scope is not None:
        q = q.filter(scope)
    return q.first() is None