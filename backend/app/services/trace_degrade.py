# -*- coding: utf-8 -*-
"""Trace 上报降级本地队列（V3-6 ）。

职责：
- `report_trace_record()`：Trace 主通道（Redis Stream `stream:trace_append`，旁路
  fan-out）上报；主通道失败 -> 幂等（delivery_key）落地本地降级队列
  `trace_local_queue`（MySQL，与 Redis 通道独立，互不叠单点）；
- `degrade_trace()`：主动降级入口（graph 写 agent_execution_logs 失败等旁路场景用，
  best-effort，绝不抛异常）；
- `drain_local_queue()`：恢复后在旁路线程重放 OPEN 记录 -> DRAINED；至少一次投递，
  采集侧以 delivery_key 幂等去重（外部副作用幂等守线）；多次失败 -> DEAD 人工介入；
- `local_queue_stats()`：观测（待消费/已消费/死亡）。

红线：
- 本模块**纯旁路**：失败只记录/入队/降级，绝不抛异常、绝不影响退款/审批主链路；
- 队列只存**已脱敏** payload（DLP：明文 PII 永不落库）；调用方负责先脱敏；
- 修改判断绝不发生在 Trace 通道内（Trace 与决策/退款完全解耦）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.models import (
    TRACE_LQ_DEAD,
    TRACE_LQ_DRAINED,
    TRACE_LQ_OPEN,
    TraceLocalQueueItem,
    new_uuid,
)
from app.infrastructure.redis_client import STREAM_TRACE_APPEND

TRACE_KIND_AGENT_LOG = "agent_log"
TRACE_KIND_STREAM_APPEND = "stream_append"
TRACE_KINDS = (TRACE_KIND_AGENT_LOG, TRACE_KIND_STREAM_APPEND)

MAX_ATTEMPTS = 5            # 重放失败上限 -> DEAD（人工介入，绝不静默丢弃）
DRAIN_LIMIT = 200


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _delivery_key(record: dict) -> str:
    """记录规范 JSON 的 sha256，作为幂等投递键（不泄业务语义）。"""
    canon = json.dumps(record, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def _app_xadd(record: dict) -> str:
    from app.infrastructure.redis_client import xadd

    return xadd(STREAM_TRACE_APPEND, {
        "delivery_key": record.get("delivery_key") or _delivery_key(record),
        "record": json.dumps(record, ensure_ascii=False),
    })


def _session(db: Session | None) -> tuple[Session, bool]:
    """db 为空时自建会话；返回 (session, 是否是自建)。"""
    if db is not None:
        return db, False
    from app.infrastructure.db import SessionLocal as _Local

    return _Local(), True


def report_trace_record(db: Session | None = None, *, kind: str = TRACE_KIND_AGENT_LOG,
                        trace_id: str | None = None, case_id: str | None = None,
                        payload: dict | None = None) -> dict:
    """Trace 上报：主通道优先；失败 -> 本地降级队列。绝不阻塞、绝不抛异常。

    payload 必须已脱敏（DLP 红线：明文 PII 永不落库）。返回如实标注 channel：
      {"reported": True,  "channel": "redis",       "delivery_key": ...}
      {"reported": False, "degraded": True, "channel": "local_queue",
       "status": "OPEN",  "id": ...}
    """
    if kind not in TRACE_KINDS:
        kind = TRACE_KIND_AGENT_LOG
    record = {
        "kind": kind,
        "trace_id": trace_id,
        "case_id": case_id,
        "ts": _now_naive().isoformat(),
        **(payload or {}),
    }
    dkey = _delivery_key(record)
    record["delivery_key"] = dkey
    try:
        _app_xadd(record)
        return {"reported": True, "channel": "redis", "delivery_key": dkey,
                "kind": kind, "trace_id": trace_id}
    except Exception as exc:  # noqa: BLE001 —— 主通道失败 -> 降级入队（旁路）
        out = degrade_to_local(db, delivery_key=dkey, kind=kind,
                               payload=record, error=str(exc))
        return {"reported": False, **out}


def degrade_to_local(db: Session | None = None, *, delivery_key: str, kind: str,
                     payload: dict, error: str | None = None) -> dict:
    """把 Trace 记录幂等落地本地降级队列（MySQL）。

    delivery_key 唯一约束保证同一条 Trace 只排一队（IntegrityError -> 幂等跳过）。
    返回如实：{"degraded": True, ...} 或失败原因（绝不抛异常打断主流程）。
    """
    s, own = _session(db)
    try:
        item = TraceLocalQueueItem(
            id=new_uuid(), delivery_key=delivery_key, kind=kind,
            payload=dict(payload), status=TRACE_LQ_OPEN,
            last_error=(error or "")[:500] if error else None,
        )
        s.add(item)
        s.commit()
        return {"degraded": True, "channel": "local_queue", "status": TRACE_LQ_OPEN,
                "id": item.id, "delivery_key": delivery_key, "error": error}
    except IntegrityError:
        s.rollback()
        return {"degraded": False, "idempotent_skip": True,
                "reason": "already_queued", "delivery_key": delivery_key}
    except Exception as exc:  # noqa: BLE001 —— 降级本身失败：只记录，不抛出
        s.rollback()
        return {"degraded": False, "error": str(exc)[:300],
                "delivery_key": delivery_key}
    finally:
        if own:
            s.close()


def degrade_trace(db: Session | None = None, *, kind: str = TRACE_KIND_AGENT_LOG,
                  trace_id: str | None = None, case_id: str | None = None,
                  payload: dict | None = None) -> dict:
    """旁路 Trace 写失败时的主动降级（如 graph._log_agent DB 写失败）。

    直接入本地队列；失败也 best-effort 返回（绝不抛异常中断主流程）。"""
    if kind not in TRACE_KINDS:
        kind = TRACE_KIND_AGENT_LOG
    record = {
        "kind": kind,
        "trace_id": trace_id,
        "case_id": case_id,
        "ts": _now_naive().isoformat(),
        "degraded_at": _now_naive().isoformat(),
        **(payload or {}),
    }
    dkey = _delivery_key(record)
    record["delivery_key"] = dkey
    return degrade_to_local(db, delivery_key=dkey, kind=kind,
                            payload=record, error="primary path unavailable")


def drain_local_queue(db: Session | None = None, *, limit: int = DRAIN_LIMIT) -> dict:
    """恢复后重放 OPEN 记录：成功 -> DRAINED（delivered_at 留痕）；失败 -> 计数+，
    超限 -> DEAD。至少一次投递，采集侧凭 delivery_key 幂等去重。"""
    s, own = _session(db)
    try:
        rows = (s.query(TraceLocalQueueItem)
                .filter(TraceLocalQueueItem.status == TRACE_LQ_OPEN)
                .order_by(TraceLocalQueueItem.enqueued_at.asc())
                .limit(limit).all())
        drained = failed = dead = 0
        for item in rows:
            try:
                _app_xadd(item.payload)
                item.status = TRACE_LQ_DRAINED
                item.delivered_at = _now_naive()
                drained += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断其余
                item.attempts = (item.attempts or 0) + 1
                item.last_error = str(exc)[:500]
                if item.attempts >= MAX_ATTEMPTS:
                    item.status = TRACE_LQ_DEAD
                    dead += 1
                else:
                    failed += 1
        s.commit()
        return {"processed": len(rows), "drained": drained, "failed": failed,
                "dead": dead, "remaining": local_queue_stats(s)["open"]}
    except Exception as exc:  # noqa: BLE001 —— 巡检异常不抛给调用方
        s.rollback()
        return {"processed": 0, "drained": 0, "failed": 0, "dead": 0,
                "error": str(exc)[:200]}
    finally:
        if own:
            s.close()


def local_queue_stats(db: Session | None = None) -> dict:
    """观测：各状态计数（只读）。"""
    s, own = _session(db)
    try:
        from sqlalchemy import func

        rows = (s.query(TraceLocalQueueItem.status,
                        func.count(TraceLocalQueueItem.id))
                .group_by(TraceLocalQueueItem.status).all())
        counts = {st or TRACE_LQ_OPEN: int(n) for st, n in rows}
        unqueued = sum(counts.get(x, 0)
                       for x in (TRACE_LQ_OPEN, TRACE_LQ_DRAINED, TRACE_LQ_DEAD))
        return {
            "open": counts.get(TRACE_LQ_OPEN, 0),
            "drained": counts.get(TRACE_LQ_DRAINED, 0),
            "dead": counts.get(TRACE_LQ_DEAD, 0),
            "total": unqueued,
        }
    finally:
        if own:
            s.close()