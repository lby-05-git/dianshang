# -*- coding: utf-8 -*-
"""Checkpointer 高可用巡检与恢复演练（V3.0 /-HA）。

职责（只读 + 演练，绝不触碰业务上下文状态）：
- `checkpoint_health()`：盘点 langgraph:checkpoint:* / checkpoint_write:* 键的数量、
  最近写入、TTL 兜底，供主管页监控检查点集群健康（单节点 Redis 故障可见）。
- `recovery_drill()`：用独立的 drill 线程合成一个检查点 -> put/read/list/delete 全链路
  演练，验证「Redis Checkpointer 写读一致 + 终态可清理」，返回 PASS 证据；演练键唯一，
  演练后必删，不回写任何真实案件键。
- `expired_keys()`：列出 TTL 已到/缺失 TTL 的键（提醒兜底异常）。只读。

红线：恢复演绝不改真实案件上下文；损坏检查点（_safe_loads 拒绝）安全失败保持；
真实案件"恢复一致且不重复退款"由既有 test_resume_http / test_workflow 守线，本轮不覆盖。
"""
from __future__ import annotations

import pickle
import time
import uuid
from datetime import datetime, timezone

import redis as redis_py

from app.core.config import settings

CKPT_PREFIX = "langgraph:checkpoint:"
WRITE_PREFIX = "langgraph:checkpoint_write:"
CKPT_TTL_SECONDS = int(60 * 60 * 24 * 7)

# V3-6：与 redis_checkpointer 同款二进制连接（pickle 需要，不能走 decode_responses=True
# 全局连接），并统一走 redis_client 的 HA 连接池（重试/超时）。
from app.infrastructure.redis_client import binary_redis_client

_binary_redis = binary_redis_client


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_loads(raw: bytes) -> tuple | None:
    """复刻 redis_checkpointer._safe_loads 的读取前校验（损坏 -> 不可用，安全失败）。"""
    try:
        obj = pickle.loads(raw)
    except Exception:  # noqa: BLE001 —— 数据不可信
        return None
    if not isinstance(obj, tuple) or len(obj) != 2:
        return None
    checkpoint, metadata = obj
    if not isinstance(checkpoint, dict) or not isinstance(metadata, dict):
        return None
    return obj


def _scan_keys(prefix: str) -> list[str]:
    out = []
    for key in _binary_redis.scan_iter(match=f"{prefix}*"):
        out.append(key.decode() if isinstance(key, bytes) else key)
    return out


def checkpoint_health() -> dict:
    """检查点集群健康盘点（只读）。"""
    ckpt_keys = _scan_keys(CKPT_PREFIX)
    write_keys = _scan_keys(WRITE_PREFIX)
    now = time.time()
    ttl_ok = missing_ttl = expired = 0
    latest_ts: float | None = None
    for key in ckpt_keys:
        ttl = _binary_redis.ttl(key)
        if ttl < 0:
            missing_ttl += 1
        elif ttl <= 0:
            expired += 1
        else:
            ttl_ok += 1
        for field_b in _binary_redis.hkeys(key):
            raw = _binary_redis.hget(key, field_b)
            if raw is None or field_b == b"latest":
                continue
            try:
                ts = pickle.loads(raw)[0].get("ts")
                if isinstance(ts, (int, float)) and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts
            except Exception:  # noqa: BLE001 —— 损坏/结构异常计为不可用
                pass
    return {
        "checked_at": _now_naive().isoformat(),
        "checkpoint_count": len(ckpt_keys),
        "write_count": len(write_keys),
        "ttl_ok": ttl_ok,
        "missing_ttl": missing_ttl,
        "expired": expired,
        "latest_write_ts": latest_ts,
        "healthy": missing_ttl == 0 and expired == 0,
    }


def _checkpoint_blob(thread_id: str) -> bytes:
    return pickle.dumps((
        {"id": "drill-ckpt", "ts": time.time(), "channel_values": {"n": 1}},
        {"source": "drill", "step": 1},
    ))


def recovery_drill() -> dict:
    """断点恢复演练：合成独立检查点 put/read/list/delete 全链路，返回 PASS 证据。

    演练键 langgraph:checkpoint:drill:{uuid} 与业务 thread 隔离，演练后必删。
    结果 = 写读一致（内容逐字节比对）+ 可列举 + 可删除；任一步失败 -> FAIL（如实）。
    """
    drill_id = f"drill-{uuid.uuid4().hex[:12]}"
    thread_id = f"{CKPT_PREFIX}{drill_id}"
    write_key = f"{WRITE_PREFIX}{drill_id}"
    outcome = []
    try:
        raw = _checkpoint_blob(thread_id)
        _binary_redis.hset(thread_id, "drill-ckpt", raw)
        _binary_redis.hset(thread_id, "latest", "drill-ckpt")
        _binary_redis.expire(thread_id, 300)
        outcome.append({"step": "put", "ok": True})

        back = _binary_redis.hget(thread_id, "drill-ckpt")
        integrity = back is not None and back == raw and _safe_loads(back) is not None
        outcome.append({"step": "read", "ok": bool(integrity)})

        listed = _binary_redis.hget(thread_id, "latest") == b"drill-ckpt"
        outcome.append({"step": "list_latest", "ok": bool(listed)})

        _binary_redis.delete(thread_id, write_key)
        gone = _binary_redis.exists(thread_id) == 0 and _binary_redis.exists(write_key) == 0
        outcome.append({"step": "delete", "ok": bool(gone)})
    except Exception as exc:  # noqa: BLE001 —— 演练异常如实 FAIL
        outcome.append({"step": "exception", "ok": False, "error": str(exc)})
        try:
            _binary_redis.delete(thread_id, write_key)
        except Exception:  # noqa: BLE001 —— 清理失败不掩盖主结果
            pass
    passed = all(o["ok"] for o in outcome)
    return {
        "drill_id": drill_id,
        "passed": passed,
        "result": "PASS" if passed else "FAIL",
        "steps": outcome,
        "note": "合成演练（drill 键隔离，与真实案件检查点无关；不触发任何退款副作用）",
        "checked_at": _now_naive().isoformat(),
    }


def expired_keys(*, limit: int = 50) -> list[str]:
    """列出 TTL 缺失/过期的检查点键（只读，提醒兜底异常）。"""
    bad = []
    for key in _scan_keys(CKPT_PREFIX) + _scan_keys(WRITE_PREFIX):
        ttl = _binary_redis.ttl(key)
        if ttl < 0:
            bad.append(key)
        if len(bad) >= limit:
            break
    return bad


def apply_ttl_fallback(ttl_seconds: int = CKPT_TTL_SECONDS) -> dict:
    """TTL 兜底修复：给所有缺失 TTL 的检查点/写队列键补上保留期（纯 add-only 维护）。

    `put()` 每次写都会续期，但历史遗留/异常路径可能留下无 TTL 键（Redis 永不过期），
    造成 langgraph:checkpoint:* 无限膨胀。修复 = 对 `ttl < 0` 的键 `expire(CKPT_TTL)`，
    不删除任何业务数据、不触碰真实退款副作用，供主管巡检/定时任务调用。
    """
    fixed = 0
    for key in _scan_keys(CKPT_PREFIX) + _scan_keys(WRITE_PREFIX):
        try:
            if _binary_redis.ttl(key) < 0:
                _binary_redis.expire(key, ttl_seconds)
                fixed += 1
        except Exception:  # noqa: BLE001 —— 个别键异常不阻断整体修复
            pass
    return {"fixed": fixed, "ttl_seconds": ttl_seconds}


def checkpointer_ha_summary() -> dict:
    """Checkpointer 高可用驾驶舱（V3-6 ：Checkpointer 不受单点影响）。

    聚合 Redis 活性 + 检查点集群健康 + 恢复演练结果（一条链如实反馈）：
    - redis_health：Redis 单节点是否可达（HA 连接池重试后仍不可达 -> 如实 DOWN）；
    - 集群健康：TTL 兜底缺失/过期键数（无单点黑洞提示）；
    - recovery_drill：合成检查点 put/read/delete 全链路演练（副作用幂等，不影响真实案件）。
    只读观测，绝不触碰真实业务上下文。
    """
    from app.infrastructure.redis_client import redis_health as _rh

    rh = _rh()
    ck = checkpoint_health()
    drill = recovery_drill()
    return {
        "redis": rh,
        "cluster": ck,
        "recovery_drill": drill,
        "overall": "OK" if (rh.get("ok") and ck.get("healthy")
                            and drill.get("passed")) else "DEGRADED",
        "note": ("检查点到 Redis 走 HA 连接池（重试/超时）；单节点不可达时读取侧安全失败"
                 "（保持 SUSPENDED 转人工），写入侧如实失败不静默吞，绝不绕过恢复机制"),
        "checked_at": _now_naive().isoformat(),
    }