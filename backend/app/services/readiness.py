# -*- coding: utf-8 -*-
"""服务就绪注册表（V3-6 -高可用：各服务独立扩缩容就绪标记）。

六大能力域（能力清单）各有**独立**的就绪标记：
  api / evaluation / intent / security / voice / sandbox

- `mark_ready()`：某实例声明就绪，写入 Redis 心跳（Set 记实例 + key TTL 续期）；
- `mark_not_ready()`：实例退出删除标记（缩容/下线）；
- `ready_snapshot()`：按服务汇总存活实例数（活跃心跳读 Sid），供 `/health/ready`
  聚合与巡检；某服务有 ≥1 存活实例即 ready（支持多实例水平扩缩容）。

红线：
- 就绪标记是**观测/编排信号**，绝不参与任何退款/审批决策（不可控的安全控制
  不会因 ready 而放宽）；
- Redis 不可用时回退进程内内存标记（只影响编排观测，不影响业务主链路）。
"""
from __future__ import annotations

import os
import socket
import uuid

from app.infrastructure.redis_client import redis_client, safe_redis

# 六大能力域（与 能力清单一致；扩缩容/健康/告警按此聚合）
SERVICES = ("api", "evaluation", "intent", "security", "voice", "sandbox")

DEFAULT_HEARTBEAT_SECONDS = 30       # 心跳 TTL：进程未续期即视为实例失活
SET_KEY_PREFIX = "svc:ready:"
HB_KEY_PREFIX = "svc:hb:"

_MEM_NOT_READY = {s: True for s in SERVICES}   # 内存兜底：Redis 不可用时伪标记空


def _key(service: str) -> str:
    return f"{SET_KEY_PREFIX}{service}"


def _hb(service: str, instance: str) -> str:
    return f"{HB_KEY_PREFIX}{service}:{instance}"


def _validate(service: str) -> None:
    if service not in SERVICES:
        raise ValueError(f"未知服务 {service!r}（可选 {SERVICES}）")


def _instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def mark_ready(service: str, *, instance: str | None = None,
               ttl_seconds: int | None = None) -> dict:
    """声明某能力域本实例就绪（Redis 心跳；多实例并发标记 -> 计数叠加）。"""
    _validate(service)
    inst = instance or _instance_id()
    ttl = ttl_seconds or DEFAULT_HEARTBEAT_SECONDS
    _MEM_NOT_READY.pop(service, None)

    def _write() -> None:
        redis_client.sadd(_key(service), inst)
        redis_client.expire(_key(service), ttl)
        redis_client.set(_hb(service, inst), "1", ex=ttl)

    safe_redis(_write)
    return {"service": service, "instance": inst, "ready": True,
            "heartbeat_ttl_seconds": ttl}


def mark_not_ready(service: str, *, instance: str | None = None) -> dict:
    """取消某实例的就绪标记（缩容/进程退出时调用）。"""
    _validate(service)
    inst = instance or _instance_id()

    def _write() -> None:
        redis_client.srem(_key(service), inst)
        redis_client.delete(_hb(service, inst))

    safe_redis(_write)
    return {"service": service, "instance": inst, "ready": False}


def ready_snapshot() -> dict:
    """按能力域汇总存活实例（只读观测，不做业务判定）。"""
    rows: list[dict] = []
    total_ready = 0

    def _count(service: str) -> int:
        members = redis_client.smembers(_key(service)) or set()
        live = 0
        for inst in members:
            if redis_client.exists(_hb(service, inst)):
                live += 1
        return live

    for svc in SERVICES:
        # Redis 不可用 -> 用内存兜底（标记过即视为就绪，仅影响编排观测）
        n = safe_redis(_count, None, svc)
        if n is None:
            n = 0 if svc in _MEM_NOT_READY else 1
        ready = n > 0
        total_ready += 1 if ready else 0
        rows.append({"service": svc, "ready": ready, "instances": int(n)})

    return {
        "overall": total_ready == len(SERVICES) and len(SERVICES) > 0,
        "ready_count": total_ready,
        "total_services": len(SERVICES),
        "services": rows,
        "checked_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None).isoformat(),
    }