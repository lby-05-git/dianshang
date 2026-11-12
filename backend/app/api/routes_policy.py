# -*- coding: utf-8 -*-
"""策略中心（3.3.4 配置与规则中心）：策略档 CRUD + 渠道绑定 + 生效档展示。

红线对齐：
  - 改阈值不破坏三态 / HITL（挂起照常，只影响「秒退/挂起」判定）；
  - leader 以上可读，admin 可写；变更走 domain_events 审计；
  - graph 决策节点通过 policy_service.resolve_policy_for_case 取渠道生效档。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_ADMIN, ROLE_LEADER, require_min_role
from app.domain.models import CHANNEL_APP, CHANNEL_CS, CHANNEL_MINI, PolicyProfile, User
from app.infrastructure.db import get_db
from app.services.policy_service import (
    _resolve_as_dict,
    bind_channel,
    get_bindings,
    list_profiles,
    policy_values,
    update_profile,
)

router = APIRouter(prefix="/policy", tags=["policy"])

CHANNELS = (CHANNEL_APP, CHANNEL_MINI, CHANNEL_CS)


class ProfileUpdateBody(BaseModel):
    amount_threshold: float | None = Field(default=None, gt=0)
    ocr_confidence: float | None = Field(default=None, ge=0, le=1.0)
    fraud_pass: int | None = Field(default=None, ge=0, le=100)
    fraud_suspend: int | None = Field(default=None, ge=0, le=100)
    blacklist_autoreject: bool | None = None
    sla_remind_hr: int | None = Field(default=None, gt=0, le=168)
    sla_escalate_hr: int | None = Field(default=None, gt=0, le=168)


class BindBody(BaseModel):
    channel: str = Field(min_length=1, max_length=20)
    profile_id: str = Field(min_length=1, max_length=36)


@router.get("/profiles")
def profiles(user: User = Depends(require_min_role(ROLE_LEADER)),
             db: Session = Depends(get_db)):
    """策略档列表（含默认档；leader+ 可读）。"""
    return {"profiles": list_profiles(db)}


@router.get("/bindings")
def bindings(user: User = Depends(require_min_role(ROLE_LEADER)),
             db: Session = Depends(get_db)):
    """渠道->策略档映射（展示当前绑定，leader+）。"""
    return {"channels": list(CHANNELS), "bindings": get_bindings(db)}


@router.get("/effective/{channel}")
def effective(channel: str, user: User = Depends(require_min_role(ROLE_LEADER)),
              db: Session = Depends(get_db)):
    """某渠道生效档（含回退逻辑到默认档），供配置页校验/联调。"""
    return {"channel": channel, "effective": policy_values(db, channel)}


@router.put("/profiles/{profile_id}")
def update(profile_id: str, body: ProfileUpdateBody,
           user: User = Depends(require_min_role(ROLE_ADMIN)), db: Session = Depends(get_db)):
    """修改策略档阈值（admin）。变更记 POLICY_UPDATED 审计。"""
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="无可更新字段")
    try:
        p = update_profile(db, profile_id, actor_id=user.id, fields=fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="策略档不存在")
    return _resolve_as_dict(p)


@router.post("/bindings", status_code=201)
def bind(body: BindBody, user: User = Depends(require_min_role(ROLE_ADMIN)),
         db: Session = Depends(get_db)):
    """渠道->策略档绑定（admin，写 POLICY_BOUND 审计）。"""
    if body.channel not in CHANNELS:
        raise HTTPException(status_code=400, detail=f"未知渠道，允许 {CHANNELS}")
    if db.get(PolicyProfile, body.profile_id) is None:
        raise HTTPException(status_code=404, detail="策略档不存在")
    bind_channel(db, body.profile_id, body.channel, actor_id=user.id)
    return {"channel": body.channel, "profile_id": body.profile_id, "action": "BOUND"}