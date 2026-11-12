# -*- coding: utf-8 -*-
"""轻量 Redis Checkpointer（docs/07 手册 ：langgraph:checkpoint:{case_id}）。

背景：langgraph-checkpoint-redis 0.5.x 依赖 redisvl（需要 RediSearch 模块），
本机 Redis 5.0.14（tporadowski Windows 版）无该模块。故基于 BaseCheckpointSaver
自实现 60 行级 Checkpointer：挂起图的完整上下文（节点栈/channel值/pending writes）
pickle 序列化进 Redis Hash，任意 Worker 凭 thread_id(=case_id) 恢复，无状态可水平扩展。

键设计：
  langgraph:checkpoint:{thread_id}           # Hash: {checkpoint_id: pickle(ckpt, meta), "latest": id}
  langgraph:checkpoint_write:{thread_id}     # Hash: {ckpt_id:task_id:task_path: pickle(writes)}
TTL 由调用方在案件挂起时设置（24h，超时告警升级，docs/06 E-16）。
"""
import logging
import pickle

import redis as redis_py
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
)

from app.core.config import settings

logger = logging.getLogger("workflow.checkpointer")

CKPT_PREFIX = "langgraph:checkpoint:"
WRITE_PREFIX = "langgraph:checkpoint_write:"

# ：检查点 TTL —— 终态案件由 runner 删除；挂起案件超时自动过期，
# 防止 langgraph:checkpoint:* 无限膨胀（配合 SLA 升级，最长保留期 < SLA 兜底）。
# 用 7 天封顶：超冠案件 SLA 升级已转人工线下处理，不依赖本检查点恢复。
CKPT_TTL_SECONDS = int(60 * 60 * 24 * 7)

# pickle 安全：Redis 曾被外部写坏/篡改时，盲目 pickle.loads 可执行任意代码；
# 读取前校验结构；损坏则视为"检查点不可用"，安全失败返回 None（由上游转人工）。
_CKPT_LOAD_ALLOWED_TYPES = None  # 预留：如需精确白名单再放开

# V3-6：改用 redis_client.binary_redis_client —— 走同一套连接池 HA/重试。
# 全局 redis_client 开了 decode_responses=True（字符串数据），pickle 二进制必须用
# 独立二进制连接，否则 0x80 协议字节会被误按 UTF-8 解码（UnicodeDecodeError）。
from app.infrastructure.redis_client import binary_redis_client

_binary_redis = binary_redis_client


def _safe_loads(raw: bytes):
    """pickle 安全读取：结构不符/反序列化失败视为损坏，安全失败。

    返回 (解析结果, 是否可用)；仅在确保可用才返回结果。避免损坏检查点导致
    盲 pickle.loads 崩溃或（更严重）反序列化不可信数据。
    """
    try:
        obj = pickle.loads(raw)
    except Exception as exc:  # noqa: BLE001 —— 数据不可信，损坏按不可用处理
        logger.warning("checkpoint corrupt (load failed): %s", exc)
        return None, False
    if not isinstance(obj, tuple) or len(obj) != 2:
        logger.warning("checkpoint corrupt (bad shape): %r", type(obj))
        return None, False
    checkpoint, metadata = obj
    if not isinstance(checkpoint, dict) or not isinstance(metadata, dict):
        logger.warning("checkpoint corrupt (field types): %r / %r",
                       type(checkpoint), type(metadata))
        return None, False
    return obj, True


class RedisHashCheckpointer(BaseCheckpointSaver):
    def __init__(self, redis_client=None):
        super().__init__()
        self.redis = redis_client or _binary_redis

    # ---- 写 ----
    def put(self, config, checkpoint, metadata, new_versions):
        thread_id = config["configurable"]["thread_id"]
        ckpt_id = checkpoint["id"]
        key = f"{CKPT_PREFIX}{thread_id}"
        self.redis.hset(key, ckpt_id, pickle.dumps((checkpoint, metadata)))
        self.redis.hset(key, "latest", ckpt_id)
        # ：TTL 兜底（终态案件跑完由 runner 删除；挂起案件至少保留 7 天）。
        # 每次写都续期，避免长时挂起中途过期。
        self.redis.expire(key, CKPT_TTL_SECONDS)
        self.redis.expire(f"{WRITE_PREFIX}{thread_id}", CKPT_TTL_SECONDS)
        return {"configurable": {"thread_id": thread_id, "checkpoint_id": ckpt_id}}

    def put_writes(self, config, writes, task_id, task_path=""):
        thread_id = config["configurable"]["thread_id"]
        ckpt_id = config["configurable"]["checkpoint_id"]
        key = f"{WRITE_PREFIX}{thread_id}"
        field = f"{ckpt_id}:{task_id}:{task_path}"
        raw = self.redis.hget(key, field)
        if raw:
            try:
                loaded = pickle.loads(raw)
            except Exception as exc:  # noqa: BLE001 —— pending writes 损坏：从空恢复，不崩溃
                logger.warning("pending_writes corrupt (load failed): %s", exc)
                loaded = []
            if not isinstance(loaded, list):
                logger.warning("pending_writes corrupt (bad type %r) — reset", type(loaded))
                loaded = []
            pending = loaded
        else:
            pending = []
        pending.extend(writes)
        self.redis.hset(key, field, pickle.dumps(pending))

    # ---- 读 ----
    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        key = f"{CKPT_PREFIX}{thread_id}"
        ckpt_id = config["configurable"].get("checkpoint_id") or self.redis.hget(key, "latest")
        if not ckpt_id:
            return None
        raw = self.redis.hget(key, ckpt_id)
        if raw is None:
            return None
        loaded, ok = _safe_loads(raw)
        if not ok:
            # ：损坏检查点不可用 -> 安全失败（视为无检查点，上游转人工）
            logger.error("checkpoint unavailable for thread=%s ckpt=%s (corrupt) -> safe fail",
                         thread_id, ckpt_id)
            return None
        checkpoint, metadata = loaded

        parent_config = None
        parent_id = checkpoint.get("parent_checkpoint_id")
        if parent_id:
            parent_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_id}}

        pending = []
        if self.redis.exists(f"{WRITE_PREFIX}{thread_id}"):
            for field_b, wraw in self.redis.hscan_iter(f"{WRITE_PREFIX}{thread_id}"):
                field = field_b.decode() if isinstance(field_b, bytes) else field_b
                fparts = field.split(":")
                if len(fparts) >= 3 and fparts[0] == ckpt_id:
                    try:
                        writes = pickle.loads(wraw)
                    except Exception as exc:  # noqa: BLE001 —— 损坏 pending 安全跳过
                        logger.warning("read pending_writes corrupt (%s) — skip", exc)
                        writes = []
                    if not isinstance(writes, list):
                        logger.warning("read pending_writes bad type %r — skip", type(writes))
                        writes = []
                    for item in writes:
                        pending.append((fparts[1], fparts[2], item))

        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_id": ckpt_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending,
        )

    def list(self, config, *, filter=None, before=None, limit=None):
        thread_id = config["configurable"]["thread_id"] if config else None
        key = f"{CKPT_PREFIX}{thread_id}"
        fields = self.redis.hkeys(key) if thread_id else []
        tuples = []
        for field_b in fields:
            field = field_b.decode() if isinstance(field_b, bytes) else field_b
            if field == "latest":
                continue
            raw = self.redis.hget(key, field)
            if raw is None:
                continue
            loaded, ok = _safe_loads(raw)
            if not ok:
                # ：损坏检查点跳过（不崩溃、不阻塞其余检查点）
                logger.error("list skip corrupt checkpoint thread=%s field=%s", thread_id, field)
                continue
            checkpoint, metadata = loaded
            cfg = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint["id"]}}
            if before and get_checkpoint_id(before) == checkpoint["id"]:
                break
            tuples.append(CheckpointTuple(config=cfg, checkpoint=checkpoint, metadata=metadata,
                                          parent_config=None, pending_writes=[]))
        tuples.sort(key=lambda t: t.config["configurable"]["checkpoint_id"], reverse=True)
        return iter(tuples[:limit] if limit else tuples)

    def delete(self, thread_id: str) -> None:
        """案件终态后清理图上下文（配合 TTL 双保险）。"""
        self.redis.delete(f"{CKPT_PREFIX}{thread_id}", f"{WRITE_PREFIX}{thread_id}")
