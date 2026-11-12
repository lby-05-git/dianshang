# -*- coding: utf-8 -*-
"""死信服务（计划 / 验收：死信可查、修复、重放不重复退款）。

死信任务 = 线上处理异常（退款/意图/评测）的兜底：查询 -> 修复 -> 人工重放。
红线（V-R5 重放不重复退款）：两层幂等——
  1. **任务级 replay_guid**：同一任务所有重放共用唯一 GUID，落入 `refund_events`
     即为其 unique(guid) 幂等键。`retry_refund(guid=…)` 重复调用审计只有一条
     REPLAY 事件，绝不产生第二笔退款/重复扣款；
  2. **重放领取原子性**：OPEN -> REPLAYING 用条件 UPDATE（rowcount==1 才算领到），
     并发重放只有一个 Worker/进程成功，其余以「已处理」幂等跳过。

语义约定（诚实记录）：
- `handler` 注入时由调用方重驱真实业务（worker 消费/进程回收），service 负责幂等防护闭环；
- 不注入 handler：仅 `refund` 任务走内置 `retry_refund`（幂等重放）；其它类型若无
  handler 明确拒绝（置回 OPEN 待修复），绝不假装重放成功。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.models import DeadLetterTask, new_uuid

STATUS_OPEN = "OPEN"
STATUS_REPLAYING = "REPLAYING"
STATUS_CLOSED = "CLOSED"
STATUS_FAILED = "FAILED"

TASK_REFUND = "refund"
TASK_INTENT = "intent"
TASK_EVAL = "eval"

_TASK_TYPES = frozenset({TASK_REFUND, TASK_INTENT, TASK_EVAL})


def _dl_guid() -> str:
    return f"dl-{uuid.uuid4().hex}"


def enqueue_dead_letter(
    db: Session, *,
    task_type: str, payload: dict | None = None, error: str | None = None,
) -> DeadLetterTask:
    """埋一条死信任务（OPEN）。task_type 限枚举，非法即异常（防错标）。"""
    if task_type not in _TASK_TYPES:
        raise ValueError(f"非法死信任务类型: {task_type}")
    row = DeadLetterTask(id=new_uuid(), task_type=task_type,
                         payload=payload, error=error,
                         status=STATUS_OPEN)
    db.add(row)
    db.commit()
    return row


def get_dead_letter(db: Session, dl_id: str) -> dict:
    row = db.get(DeadLetterTask, dl_id)
    if row is None:
        raise KeyError(f"dead letter {dl_id} not found")
    return _as_dict(row)


def list_dead_letters(
    db: Session, *, status: str | None = None, task_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """死信工作台查询（可筛状态/类型）。"""
    q = db.query(DeadLetterTask)
    if status:
        if status not in (STATUS_OPEN, STATUS_REPLAYING, STATUS_CLOSED, STATUS_FAILED):
            raise ValueError(f"非法状态筛选: {status}")
        q = q.filter_by(status=status)
    if task_type:
        q = q.filter_by(task_type=task_type)
    rows = q.order_by(DeadLetterTask.created_at.desc()).limit(limit).all()
    return [_as_dict(r) for r in rows]


def repair_dead_letter(
    db: Session, dl_id: str, *,
    task_type: str | None = None, payload: dict | None = None,
    error: str | None = None, reopen: bool = False,
) -> dict:
    """修复：改 task_type/payload/error，或把 FAILED 重新 OPEN（修复后重试前置）。"""
    row = db.get(DeadLetterTask, dl_id)
    if row is None:
        raise KeyError(f"dead letter {dl_id} not found")
    if task_type is not None:
        if task_type not in _TASK_TYPES:
            raise ValueError(f"非法任务类型: {task_type}")
        row.task_type = task_type
    if payload is not None:
        row.payload = payload
    if error is not None:
        row.error = error
    if reopen and row.status == STATUS_FAILED:
        row.status = STATUS_OPEN
        row.error = None
    db.commit()
    return _as_dict(row)


def claim_for_replay(db: Session, dl_id: str) -> str | None:
    """原子领取：OPEN -> REPLAYING + 固定 replay_guid。

    返回该次重放的 GUID；未抢到（已 CLOSED/REPLAYING/FAILED 或不存在）返回 None。
    条件 UPDATE 保证并发下只有一个调用方领到，其余幂等跳过 —— 不重复退款的第一道锁。
    """
    row = db.get(DeadLetterTask, dl_id)
    if row is None or row.status != STATUS_OPEN:
        return None
    guid = row.replay_guid or _dl_guid()
    updated = (
        db.query(DeadLetterTask)
        .filter(DeadLetterTask.id == dl_id, DeadLetterTask.status == STATUS_OPEN)
        .update({"status": STATUS_REPLAYING, "replay_guid": guid},
                synchronize_session="fetch")
    )
    db.commit()
    if updated:
        db.refresh(row)   # 同步原始 UPDATE 后的 replay_guid/status，避免陈旧快照
    return guid if updated else None


def _close(db: Session, dl_id: str, *, ok: bool, error: str | None = None,
           replayed_at: datetime | None = None) -> None:
    row = db.get(DeadLetterTask, dl_id)
    if row is None:
        return
    row.status = STATUS_CLOSED if ok else STATUS_FAILED
    row.replayed_ok = ok
    if error is not None:
        row.error = error
    if replayed_at is not None:
        row.replayed_at = replayed_at
    db.commit()


def replay_dead_letter(
    db: Session, dl_id: str, *,
    replayed_by: str | None = None, handler=None,
) -> dict:
    """人工/系统重放死信（幂等，不重复退款）。

    - 领取成功才重驱；returner_key（replay_guid）跨进程复用，第二道幂等锁在 `refund_events`；
    - `handler(db, task_dict)` 注入时由调用方重驱真实业务；
    - refund 任务无 handler 时走内置 `retry_refund`（同一 GUID，审计唯一，绝不重复退款）；
    - 其它任务类型无 handler -> 拒绝并置回 OPEN（诚实：不假装成功）。
    """
    row = db.get(DeadLetterTask, dl_id)
    if row is None:
        raise KeyError(f"dead letter {dl_id} not found")
    if row.status == STATUS_CLOSED and row.replayed_ok:
        return {"id": dl_id, "replayed": False, "skipped": "already_closed",
                "replay_guid": row.replay_guid}
    if row.status == STATUS_REPLAYING:
        # 正被并发领取/重放：幂等跳过（另一路在处理）
        return {"id": dl_id, "replayed": False, "skipped": "in_progress",
                "replay_guid": row.replay_guid}

    guid = claim_for_replay(db, dl_id)
    if guid is None:
        return {"id": dl_id, "replayed": False, "skipped": "not_claimable",
                "replay_guid": row.replay_guid}
    if handler is None:
        if row.task_type == TASK_REFUND:
            case_id = (row.payload or {}).get("case_id")
            if not case_id:
                _close(db, dl_id, ok=False,
                       error="refund payload 缺 case_id，重放前需修复")
                return {"id": dl_id, "replayed": False, "skipped": "missing_case_id",
                        "replay_guid": guid}
            # 幂等重放：同一 guid 只落一条 REPLAY 事件 —— 第二道不重复退款锁
            try:
                from app.services.refund_events import retry_refund
                retry_refund(db, case_id, guid=guid,
                             reason=f"dead-letter replay by {replayed_by or 'system'}")
            except Exception as exc:  # noqa: BLE001 —— 案件不存在/重放失败留痕 FAILED
                _close(db, dl_id, ok=False, error=f"replay failed: {exc}")
                return {"id": dl_id, "replayed": False, "replay_guid": guid,
                        "error": str(exc)}
            _close(db, dl_id, ok=True, replayed_at=datetime.now())
            return {"id": dl_id, "replayed": True, "replay_guid": guid,
                    "case_id": case_id}
        # 非 refund 且无 handler：拒绝，不能假装重放成功
        _close(db, dl_id, ok=False, error="非 refund 任务重放需注入 handler 重驱业务")
        raise ValueError(
            f"task_type={row.task_type} 重放需要 handler（死信 {dl_id} 无处理逻辑）")
    try:
        handler(db, _as_dict(row, replay_guid=guid))
        _close(db, dl_id, ok=True, replayed_at=datetime.now())
        return {"id": dl_id, "replayed": True, "replay_guid": guid}
    except Exception as exc:  # noqa: BLE001 —— 重放失败留痕 FAILED，可修复后再试
        _close(db, dl_id, ok=False, error=f"replay failed: {exc}")
        return {"id": dl_id, "replayed": False, "replay_guid": guid,
                "error": str(exc)}


def _as_dict(r: DeadLetterTask, *, replay_guid: str | None = None) -> dict:
    return {
        "id": r.id,
        "task_type": r.task_type,
        "payload": r.payload,
        "error": r.error,
        "status": r.status,
        "replayed_ok": r.replayed_ok,
        "replay_guid": replay_guid or r.replay_guid,
        "replayed_at": r.replayed_at.isoformat() if r.replayed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }