# -*- coding: utf-8 -*-
"""V3.0 V3-3 版本注册中心（7 类实体版本）。

管理 Agent / Prompt / 模型 / 工具 / 权限 / 数据集 / 安全策略 的统一版本：
- register   登记新版本（DRAFT）；重复 (entity_type, entity_name, version) 拒绝；
- approve    批准（leader+，DRAFT/CANDIDATE → APPROVED，留 approver/时间）；
- reject     否决（DRAFT/CANDIDATE → REJECTED）；
- rollback   回滚关联（ROOLED_BACK + rollback_to 指向已批准的历史版本号；只留痕）；
- history    某实体全部版本（newest first）；
- latest_approved 某实体当前生效（最近已批准）版本。

红线（对齐 V2.0 版本治理纪律）：
- 本模块只写版本/批准/回滚留痕，绝不静默改任何策略、工具行为或业务判定；
- Prompt 全文归 prompt_versions 表；本表 content 只存 sha256 指纹与脱敏元数据（payload）；
- 每次状态变更落一条 DomainEvent 审计（append-only）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import (
    VERSION_ENTITY_TYPES,
    VERSION_STATUS_APPROVED,
    VERSION_STATUS_CANDIDATE,
    VERSION_STATUS_DRAFT,
    VERSION_STATUS_REJECTED,
    VERSION_STATUS_ROLLED_BACK,
    VersionRegistry,
    new_uuid,
)
from app.infrastructure.audit import write_event

# ---- 审计动作 ----#
ACT_VERSION_REGISTERED = "VERSION_REGISTERED"
ACT_VERSION_APPROVED = "VERSION_APPROVED"
ACT_VERSION_REJECTED = "VERSION_REJECTED"
ACT_VERSION_ROLLED_BACK = "VERSION_ROLLED_BACK"

# 可批准状态：DRAFT/CANDIDATE -> APPROVED
_APPROVABLE = (VERSION_STATUS_DRAFT, VERSION_STATUS_CANDIDATE)
# 可回滚状态：仅已批准（ACTIVE 生效）版本可被回滚（回滚标记 ROLLED_BACK）
_ROLLBACKABLE = (VERSION_STATUS_APPROVED,)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot(reg: VersionRegistry) -> dict:
    return {
        "id": reg.id,
        "entity_type": reg.entity_type,
        "entity_name": reg.entity_name,
        "version": reg.version,
        "status": reg.status,
        "content_sha256": reg.content_sha256,
        "payload": reg.payload or {},
        "changelog": reg.changelog,
        "creator_id": reg.creator_id,
        "approver_id": reg.approver_id,
        "approved_at": reg.approved_at.isoformat() if reg.approved_at else None,
        "rollback_to": reg.rollback_to,
        "created_at": reg.created_at.isoformat() if reg.created_at else None,
    }


def list_versions(
    db: Session, *,
    entity_type: str | None = None, entity_name: str | None = None,
    status: str | None = None, limit: int = 50,
) -> list[dict]:
    q = db.query(VersionRegistry)
    if entity_type:
        q = q.filter(VersionRegistry.entity_type == entity_type)
    if entity_name:
        q = q.filter(VersionRegistry.entity_name == entity_name)
    if status:
        q = q.filter(VersionRegistry.status == status)
    rows = (q.order_by(VersionRegistry.created_at.desc(), VersionRegistry.id.desc())
            .limit(limit).all())
    return [_snapshot(r) for r in rows]


def history(db: Session, *, entity_type: str, entity_name: str,
            limit: int = 50) -> dict:
    rows = list_versions(db, entity_type=entity_type, entity_name=entity_name,
                         limit=limit)
    latest = None
    for r in rows:
        if r["status"] == VERSION_STATUS_APPROVED:
            latest = r
            break
    return {"entity_type": entity_type, "entity_name": entity_name,
            "latest_approved": latest, "versions": rows}


def register(
    db: Session, *,
    entity_type: str, entity_name: str, version: str,
    content: str | None = None, payload: dict | None = None,
    changelog: str | None = None, creator_id: str | None = None,
) -> dict:
    """登记新版本（DRAFT）。实体类型/版本号格式非法 -> ValueError。"""
    if entity_type not in VERSION_ENTITY_TYPES:
        raise ValueError(
            f"非法实体类型 {entity_type}（可选 {VERSION_ENTITY_TYPES}）")
    if not entity_name or not entity_name.strip():
        raise ValueError("实体名不能为空")
    if not version or not version.strip():
        raise ValueError("版本号不能为空")
    dup = (db.query(VersionRegistry)
           .filter_by(entity_type=entity_type, entity_name=entity_name,
                      version=version).first())
    if dup is not None:
        raise ValueError(f"版本已存在: {entity_type}/{entity_name}@{version}")
    content_sha = None
    if content is not None:
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    reg = VersionRegistry(
        id=new_uuid(),
        entity_type=entity_type, entity_name=entity_name, version=version,
        status=VERSION_STATUS_DRAFT,
        content_sha256=content_sha,
        payload=payload,
        changelog=changelog, creator_id=creator_id,
    )
    db.add(reg)
    db.flush()
    write_event(db, actor_id=creator_id, action=ACT_VERSION_REGISTERED,
                payload={"entity_type": entity_type, "entity_name": entity_name,
                         "version": version})
    db.commit()
    return _snapshot(reg)


def approve(db: Session, *, reg_id: str, approver_id: str | None = None) -> dict:
    """批准版本（仅 DRAFT/CANDIDATE -> APPROVED）。非法状态 -> ValueError。"""
    reg = db.get(VersionRegistry, reg_id)
    if reg is None:
        raise KeyError(reg_id)
    if reg.status not in _APPROVABLE:
        raise ValueError(
            f"状态 {reg.status} 不可批准（仅 {_APPROVABLE}）")
    reg.status = VERSION_STATUS_APPROVED
    reg.approver_id = approver_id
    reg.approved_at = _now()
    db.flush()
    write_event(db, actor_id=approver_id, action=ACT_VERSION_APPROVED,
                payload={"entity_type": reg.entity_type,
                         "entity_name": reg.entity_name, "version": reg.version})
    db.commit()
    return _snapshot(reg)


def reject(db: Session, *, reg_id: str, approver_id: str | None = None) -> dict:
    """否决版本（DRAFT/CANDIDATE -> REJECTED）。非法状态 -> ValueError。"""
    reg = db.get(VersionRegistry, reg_id)
    if reg is None:
        raise KeyError(reg_id)
    if reg.status not in _APPROVABLE:
        raise ValueError(f"状态 {reg.status} 不可否决（仅 {_APPROVABLE}）")
    reg.status = VERSION_STATUS_REJECTED
    reg.approver_id = approver_id
    db.flush()
    write_event(db, actor_id=approver_id, action=ACT_VERSION_REJECTED,
                payload={"entity_type": reg.entity_type,
                         "entity_name": reg.entity_name, "version": reg.version})
    db.commit()
    return _snapshot(reg)


def rollback(db: Session, *, reg_id: str, target_version: str,
             approver_id: str | None = None) -> dict:
    """回滚关联：把某已批准版本标记 ROLLED_BACK，指向一个已批准的历史版本。

    - reg 必须是 APPROVED（生效中版本才能被回滚）；
    - target_version 必须存在且为 APPROVED（只能回滚到已批准基准），且不同版本；
    - 只留痕（rollback_to），绝不静默改任何策略/工具行为（由版本中心消费方显式执行）。
    """
    reg = db.get(VersionRegistry, reg_id)
    if reg is None:
        raise KeyError(reg_id)
    if reg.status not in _ROLLBACKABLE:
        raise ValueError(
            f"状态 {reg.status} 不可回滚（仅已批准 {_ROLLBACKABLE}）")
    if target_version == reg.version:
        raise ValueError("回滚目标不能是当前版本")
    target = (db.query(VersionRegistry)
              .filter_by(entity_type=reg.entity_type, entity_name=reg.entity_name,
                         version=target_version).first())
    if target is None:
        raise ValueError(f"回滚目标版本不存在: {target_version}")
    if target.status != VERSION_STATUS_APPROVED:
        raise ValueError(f"回滚目标须为已批准版本（当前 {target.status}）")
    reg.status = VERSION_STATUS_ROLLED_BACK
    reg.rollback_to = target_version
    reg.approver_id = approver_id
    db.flush()
    write_event(db, actor_id=approver_id, action=ACT_VERSION_ROLLED_BACK,
                payload={"entity_type": reg.entity_type,
                         "entity_name": reg.entity_name,
                         "version": reg.version, "rollback_to": target_version})
    db.commit()
    return _snapshot(reg)


def latest_approved(db: Session, *, entity_type: str, entity_name: str) -> dict | None:
    """某实体当前生效版本（最近一次已批准；未批准过 -> None）。"""
    row = (db.query(VersionRegistry)
           .filter_by(entity_type=entity_type, entity_name=entity_name,
                      status=VERSION_STATUS_APPROVED)
           .order_by(VersionRegistry.created_at.desc(), VersionRegistry.id.desc())
           .first())
    return None if row is None else _snapshot(row)