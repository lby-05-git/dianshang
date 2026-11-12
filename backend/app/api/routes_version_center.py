# -*- coding: utf-8 -*-
"""V3.0 V3-3 版本注册中心 API。

`/api/v1/governance/versions`：
  - GET    /domains                      实体类型清单（agent/prompt/model/tool/permission/dataset/security_policy）
  - GET    /?entity_type=&entity_name=&status=   版本列表（只读，认证即可）
  - GET    /history?entity_type=&entity_name=    单实体版本史 + 当前生效版本
  - POST   /                             登记新版本（DRAFT，leader+）
  - POST   /{id}/approve                 批准（DRAFT/CANDIDATE -> APPROVED，leader+）
  - POST   /{id}/reject                  否决（DRAFT/CANDIDATE -> REJECTED，leader+）
  - POST   /{id}/rollback                回滚关联（标记 ROLLED_BACK + rollback_to，leader+）

红线：版本留痕只增不改；绝不静默改策略/工具行为/业务判定。每次状态变更落审计。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_LEADER, get_current_user, require_min_role
from app.domain.models import VERSION_ENTITY_TYPES, User
from app.infrastructure.db import get_db
from app.services import version_center as vc

router = APIRouter(prefix="/governance/versions", tags=["governance"])


class VersionCreateBody(BaseModel):
    entity_type: str = Field(min_length=1, max_length=20)
    entity_name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    content: str | None = Field(default=None, max_length=20000,
                                description="内容全文（仅取 sha256 指纹；Prompt 全文仍走 prompt_versions）")
    payload: dict | None = Field(default=None)
    changelog: str | None = Field(default=None, max_length=2000)


class VersionRollbackBody(BaseModel):
    target_version: str = Field(min_length=1, max_length=40)


@router.get("/domains")
def versions_domains(user: User = Depends(get_current_user)):
    """实体类型清单（只读）。"""
    return {"domains": list(VERSION_ENTITY_TYPES)}


@router.get("")
def versions_list(
    entity_type: str | None = None,
    entity_name: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """版本列表（只读，newest first）。"""
    return {"items": vc.list_versions(db, entity_type=entity_type,
                                      entity_name=entity_name, status=status,
                                      limit=limit)}


@router.get("/history")
def versions_history(
    entity_type: str = Query(min_length=1, max_length=20),
    entity_name: str = Query(min_length=1, max_length=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单实体版本史 + 当前生效版本（只读）。"""
    return {"item": vc.history(db, entity_type=entity_type, entity_name=entity_name)}


@router.post("", status_code=201)
def versions_create(
    payload: VersionCreateBody,
    user: User = Depends(require_min_role(ROLE_LEADER)),
    db: Session = Depends(get_db),
):
    """登记新版本（leader+）。"""
    try:
        item = vc.register(db, entity_type=payload.entity_type,
                           entity_name=payload.entity_name, version=payload.version,
                           content=payload.content, payload=payload.payload,
                           changelog=payload.changelog, creator_id=str(user.id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"item": item}


@router.post("/{reg_id}/approve")
def versions_approve(
    reg_id: str,
    user: User = Depends(require_min_role(ROLE_LEADER)),
    db: Session = Depends(get_db),
):
    """批准版本（leader+）。"""
    try:
        item = vc.approve(db, reg_id=reg_id, approver_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="版本不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"item": item}


@router.post("/{reg_id}/reject")
def versions_reject(
    reg_id: str,
    user: User = Depends(require_min_role(ROLE_LEADER)),
    db: Session = Depends(get_db),
):
    """否决版本（leader+）。"""
    try:
        item = vc.reject(db, reg_id=reg_id, approver_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="版本不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"item": item}


@router.post("/{reg_id}/rollback")
def versions_rollback(
    reg_id: str,
    body: VersionRollbackBody,
    user: User = Depends(require_min_role(ROLE_LEADER)),
    db: Session = Depends(get_db),
):
    """回滚关联（leader+）：只标记 ROLLED_BACK + rollback_to，绝不静默改策略。"""
    try:
        item = vc.rollback(db, reg_id=reg_id, target_version=body.target_version,
                           approver_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="版本不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"item": item}