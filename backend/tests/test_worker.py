# -*- coding: utf-8 -*-
"""Phase 4 验证：可靠消费/ACK、失败重试转死信、幂等跳过、案件锁、pending 回收。

注意：本组测试直接操作开发 Redis（D:/Redis，6379），使用独立 stream 名避免与业务数据混用。
"""
import uuid
import time

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    DECISION_APPROVED,
    DECISION_PENDING,
    AgentExecutionLog,
    RefundCase,
    User,
)
from app.infrastructure.redis_client import redis_client
from app.infrastructure.seed import seed
from app.worker.base import RedisStreamConsumer
from tests.conftest import SessionTest


def _unique_stream(tag: str) -> str:
    return f"stream:test:{tag}:{uuid.uuid4().hex[:8]}"


def _add_user(s) -> str:
    u = User(username=f"wk_{uuid.uuid4().hex[:8]}", password_hash="x", role="customer_service")
    s.add(u)
    s.flush()
    return u.id


class _CollectConsumer(RedisStreamConsumer):
    """记录被处理的消息；可注入失败函数。"""

    def __init__(self, stream: str, group: str, fail_on: callable = None):
        self.fail_on = fail_on
        self.handled: list[tuple[str, dict]] = []
        self.stream = stream
        self.group = group
        super().__init__()
        self._consumer = "test-c"

    def process(self, message_id: str, fields: dict) -> None:
        if self.fail_on and self.fail_on(fields):
            raise RuntimeError("injected failure")
        self.handled.append((message_id, fields))


def _make_case(s, *, status="RUNNING", decision="PENDING") -> str:
    uid = _add_user(s)
    c = RefundCase(order_id=f"ORD-{uuid.uuid4().hex[:8]}", requested_amount=128,
                   submitter_id=uid, current_status=status, final_decision=decision)
    s.add(c)
    s.commit()
    return c.id


def test_consume_and_ack():
    stream, group = _unique_stream("ack"), "g"
    mid = redis_client.xadd(stream, {"case_id": "c1"})
    c = _CollectConsumer(stream, group)
    c._handle(mid, {"case_id": "c1"})
    assert c.handled and c.handled[0][1]["case_id"] == "c1"
    assert redis_client.xpending(stream, group)["pending"] == 0  # 已 ACK


def test_failure_retries_then_dlq():
    stream, group = _unique_stream("dlq"), "g"
    mid = redis_client.xadd(stream, {"case_id": "c1"})
    c = _CollectConsumer(stream, group)
    c.retries = 2
    c.backoff_base = 0.01
    c.fail_on = lambda f: True
    c._handle(mid, {"case_id": "c1"})
    # 原消息已 ACK（转死信），死信流里有记录
    assert redis_client.xpending(stream, group)["pending"] == 0
    dlq_entries = redis_client.xrange("stream:dlq", "-", "+")
    assert any("c1" in (e[1].get("case_id", "")) for e in dlq_entries)


def test_idempotent_skip_for_terminal_case():
    """已终态案件：消费应跳过且不留副作用（不重复写 intake 日志）。"""
    stream, group = _unique_stream("idem"), "g"
    s = SessionTest()
    seed(db=s)
    cid = _make_case(s, status="COMPLETED", decision="APPROVED")
    s.close()

    mid = redis_client.xadd(stream, {"case_id": cid})
    from app.worker.refund_processor import RefundApplyConsumer

    consumer = RefundApplyConsumer()
    consumer.stream, consumer.group = stream, group
    consumer._consumer = "test-idem"
    consumer._ensure_group()
    consumer._handle(mid, {"case_id": cid})

    s2 = SessionTest()
    logs = s2.query(AgentExecutionLog).filter_by(case_id=cid).count()
    s2.close()
    assert logs == 0
    assert redis_client.xpending(stream, group)["pending"] == 0


def test_case_lock_prevents_concurrent_processing():
    """案件锁：锁被占时消费不处理也不 ACK（留给 pending 回收）。"""
    stream, group = _unique_stream("lock"), "g"
    redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    mid = redis_client.xadd(stream, {"case_id": "c-lock"})
    # 先由"其他 Worker"读走消息（进入 PEL）但未 ACK
    redis_client.xreadgroup(group, "other-worker", {stream: ">"}, count=1)
    redis_client.set("lock:case:c-lock", "other-worker", nx=True, ex=60)
    c = _CollectConsumer(stream, group)
    c._handle(mid, {"case_id": "c-lock"})
    assert c.handled == []                      # 未处理
    assert redis_client.xpending(stream, group)["pending"] == 1  # 未 ACK
    # 释放锁后回收可继续处理
    redis_client.delete("lock:case:c-lock")
    c.reclaim_min_idle = 0
    c._reclaim()
    assert len(c.handled) == 1
    assert redis_client.xpending(stream, group)["pending"] == 0


def test_reclaim_redelivers_pending_after_crash():
    """模拟 Worker 崩溃：消息已读未 ACK -> 回收重投 -> 处理并 ACK。"""
    stream, group = _unique_stream("reclaim"), "g"
    redis_client.xgroup_create(stream, group, id="0", mkstream=True)
    mid = redis_client.xadd(stream, {"case_id": "c-reclaim"})
    redis_client.xreadgroup(group, "crashed-worker", {stream: ">"}, count=1)  # 读走但故意不 ACK
    assert redis_client.xpending(stream, group)["pending"] == 1

    c = _CollectConsumer(stream, group)
    c.reclaim_min_idle = 0
    c._reclaim()
    assert c.handled and c.handled[0][1]["case_id"] == "c-reclaim"
    assert redis_client.xpending(stream, group)["pending"] == 0
