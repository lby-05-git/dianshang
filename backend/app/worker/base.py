# -*- coding: utf-8 -*-
"""Stream 消费者基类（docs/08 第4阶段）。

可靠性机制：
- 消费组 XREADGROUP（BLOCK 5s），成功 XACK；
- 失败重试 N 次（指数退避）→ 转死信 stream:dlq + 案件 SYSTEM_FAILURE；
- Worker 崩溃后未 ACK 消息留在 PEL，由 _reclaim()（XPENDING+XCLAIM）重投；
- 同一案件并发防护：Redis SETNX 案件锁（Lua 校验释放），拿不到锁则不 ACK 留给回收；
- Redis 断连：指数退避重连，不崩溃。
"""
import logging
import os
import time
import uuid

from app.infrastructure.redis_client import STREAM_DLQ, redis_client

logger = logging.getLogger("worker.base")

# Redis 5.0 无 XAUTOCLAIM，用 XPENDING + XCLAIM 实现 pending 回收
_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class RedisStreamConsumer:
    stream: str = ""
    group: str = ""
    retries: int = 3
    backoff_base: float = 1.0          # 1s/2s/4s
    reclaim_every: float = 10.0        # 秒
    reclaim_min_idle: int = 30000      # 毫秒
    case_lock_ttl: int = 60

    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or f"{self.group}-{os.getpid()}"
        self._consumer = f"{self.group}-{uuid.uuid4().hex[:6]}"
        self._ensure_group()

    # ---- 子类实现 ----
    def process(self, message_id: str, fields: dict) -> None:
        """处理单条消息；抛出异常将进入重试/死信逻辑。"""
        raise NotImplementedError

    # ---- 消费循环 ----
    def run(self, stop_event=None) -> None:
        last_reclaim = time.monotonic()
        while True:
            if stop_event and stop_event.is_set():
                logger.info("consumer %s stopped", self.group)
                return
            try:
                items = redis_client.xreadgroup(
                    # Redis 客户端 socket_timeout=2s；BLOCK 必须更短，否则空闲队列
                    # 每轮都会被误报为 Timeout reading from socket。
                    self.group, self._consumer, {self.stream: ">"}, count=10, block=1000
                )
                for _stream, msgs in items or []:
                    for mid, fields in msgs:
                        self._handle(mid, fields)
                if time.monotonic() - last_reclaim > self.reclaim_every:
                    self._reclaim()
                    last_reclaim = time.monotonic()
            except Exception as exc:  # noqa: BLE001 —— Redis 断连等：退避重连
                logger.warning("consumer %s error: %s; retry in 2s", self.group, exc)
                time.sleep(2)

    # ---- 单条消息处理 ----
    def _handle(self, message_id: str, fields: dict) -> None:
        case_id = fields.get("case_id")
        if not case_id:
            self._ack(message_id)
            return
        owner = f"{self.group}:{message_id}:{self._consumer}"
        if not redis_client.set(f"lock:case:{case_id}", owner, nx=True, ex=self.case_lock_ttl):
            # 其他 Worker 正在处理同一案件 → 不 ACK，留给 pending 回收重投
            return
        try:
            for attempt in range(self.retries):
                try:
                    self.process(message_id, fields)
                    self._ack(message_id)
                    return
                except Exception as exc:  # noqa: BLE001
                    if attempt < self.retries - 1:
                        wait = self.backoff_base * (2 ** attempt)
                        logger.warning("retry %s msg=%s in %.1fs: %s", self.group, message_id, wait, exc)
                        time.sleep(wait)
                    else:
                        self._dlq(message_id, fields, exc)
                        return
        finally:
            redis_client.eval(_LOCK_LUA, 1, f"lock:case:{case_id}", owner)

    # ---- 基础设施 ----
    def _ensure_group(self) -> None:
        try:
            redis_client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP
            if "BUSYGROUP" not in str(exc):
                logger.warning("xgroup create %s: %s", self.group, exc)

    def _ack(self, message_id: str) -> None:
        redis_client.xack(self.stream, self.group, message_id)

    def _dlq(self, message_id: str, fields: dict, error: Exception) -> None:
        logger.error("DLQ %s msg=%s err=%s", self.group, message_id, error)
        redis_client.xadd(STREAM_DLQ, {
            "stream": self.stream, "group": self.group, "mid": message_id,
            "case_id": fields.get("case_id", ""), "error": str(error)[:500],
            "worker": self.worker_id,
        })
        self._ack(message_id)  # 原消息确认，避免 PEL 无限重投
        self._mark_system_failure(fields.get("case_id", ""))

    def _mark_system_failure(self, case_id: str) -> None:
        """兜底：仅 RUNNING+PENDING 的案件可置 SYSTEM_FAILURE（状态机校验）。"""
        try:
            from app.domain.models import (
                CASE_STATUS_COMPLETED,
                CASE_STATUS_RUNNING,
                DECISION_PENDING,
                DECISION_SYSTEM_FAILURE,
                RefundCase,
            )
            from app.domain.state_machine import transition_case
            from app.infrastructure.db import SessionLocal

            db = SessionLocal()
            try:
                case = db.get(RefundCase, case_id)
                if case is None:
                    return
                if case.final_decision != DECISION_PENDING or case.current_status != CASE_STATUS_RUNNING:
                    return
                ns, nd = transition_case(
                    current_status=case.current_status, current_decision=case.final_decision,
                    new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_SYSTEM_FAILURE,
                )
                case.current_status = ns
                case.final_decision = nd
                db.commit()
                logger.warning("case %s -> SYSTEM_FAILURE (worker dlq)", case_id)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 —— 兜底标记失败不阻塞消费循环
            logger.exception("mark system failure failed for %s", case_id)

    def _reclaim(self) -> None:
        """回收 PEL 中超时未 ACK 的消息（Worker 崩溃后重投）。

        空闲判定交给服务端 XCLAIM min_idle_time（权威），客户端不做近似判断。
        """
        try:
            pending = redis_client.xpending_range(self.stream, self.group, min="-", max="+", count=100)
            if not pending:
                return
            mids = [p["message_id"] for p in pending]
            claimed = redis_client.xclaim(
                self.stream, self.group, self._consumer, self.reclaim_min_idle, mids, justid=False,
            )
            for mid, fields in claimed:
                self._handle(mid, dict(fields))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reclaim error: %s", exc)
