# -*- coding: utf-8 -*-
"""V3-6 -4：Trace 上报降级本地队列 —— 故障注入：退款主流程不阻塞 + 降级入队。

覆盖：
- D1 主通道正常：report_trace_record 走 Redis Stream，reported=True；
- D2 主通道故障：xadd 抛错 -> 降级入 trace_local_queue（OPEN），主流程不抛异常、
  不阻塞（调用方照常往下走）；
- D3 幂等：同 delivery_key 再入队 -> idempotent_skip，不重复；
- D4 降级本身失败：数据库故障时 best-effort 返回，绝不抛给主流程；
- D5 drain 重放：恢复后重放 OPEN -> DRAINED（至少一次投递，采集侧幂等）；
- D6 graph 旁路写失败：_log_agent DB 失败 -> 调 degrade_trace 降级（不中断）；
- HA 基础设施：redis_health / safe_redis 故障窗口 / readiness 扩缩容 / /health/ready。

红线：降级/跳过/未测得一律如实标注；明文 PII 不入队（payload 先脱敏）；绝不影响退款。

注意：trace_local_queue 为持久测试库，测试用唯一 delivery_key + 结束清理，不做绝对行数断言。
"""
import time

import pytest
import redis as redis_py

from app.domain.models import TRACE_LQ_OPEN, TraceLocalQueueItem
from app.infrastructure import redis_client as rc
from app.infrastructure.redis_client import STREAM_TRACE_APPEND
from app.services import trace_degrade as td
from tests.conftest import SessionTest


def _uniq(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}"


def _clean_queue(s, delivery_key_prefix: str) -> None:
    """清理本测试产生的降级行（前缀匹配，不误删他人）。"""
    rows = (s.query(TraceLocalQueueItem)
            .filter(TraceLocalQueueItem.delivery_key.like(f"{delivery_key_prefix}%"))
            .delete(synchronize_session=False))
    s.commit()
    return rows


def _clean_stream(delivery_key: str) -> None:
    """删除本测试写入 Redis Stream 的条目（按 delivery_key 匹配）。"""
    for entry in rc.redis_client.xrevrange(STREAM_TRACE_APPEND, count=50):
        fields = entry[1]
        if fields.get("delivery_key") == delivery_key:
            rc.redis_client.xdel(STREAM_TRACE_APPEND, entry[0])


# ---- D1 主通道正常 ----

def test_primary_redis_channel_success():
    s = SessionTest()
    try:
        out = td.report_trace_record(s, kind="agent_log", trace_id="t-main",
                                     payload={"event": "applied", "amount": "***"})
        assert out["reported"] is True and out["channel"] == "redis"
        assert out["delivery_key"]
        _clean_stream(out["delivery_key"])
    finally:
        s.close()


# ---- D2 主通道故障 -> 降级入队，主流程不阻塞 ----

def test_redis_failure_degrades_and_main_flow_not_blocked():
    s = SessionTest()
    dk_prefix = _uniq("dk-d2")
    try:
        orig = rc.xadd

        def _boom(*a, **k):
            raise redis_py.ConnectionError("forced redis down")

        rc.xadd = _boom
        try:
            # 模拟退款主流程：副链路 Trace 上报失败绝不能阻塞主流程后续动作
            steps: list[str] = []
            out = td.report_trace_record(
                s, kind="stream_append", trace_id=_uniq("trace"),
                case_id="c-d2", payload={"event": "refund-applied",
                                          "refund_id": "R-XXX", "masked": "***"})
            assert out["reported"] is False
            assert out["degraded"] is True and out["status"] == TRACE_LQ_OPEN
            steps.append("refund_applied")   # 主流程照常完成
            assert steps == ["refund_applied"]
        finally:
            rc.xadd = orig

        row = (s.query(TraceLocalQueueItem)
               .filter(TraceLocalQueueItem.delivery_key == out["delivery_key"])
               .first())
        assert row is not None
        assert row.status == TRACE_LQ_OPEN
        payload = row.payload or {}
        assert payload["event"] == "refund-applied"
        # 明文 PII 不入队：已在调用侧脱敏，字段为打码值
        assert "***" in str(payload)
    finally:
        _clean_queue(s, dk_prefix)
        s.close()


# ---- D3 幂等 ----

def test_degrade_idempotent_delivery_key():
    s = SessionTest()
    dk = _uniq("dk-d3")
    try:
        r1 = td.degrade_to_local(s, delivery_key=dk, kind="agent_log",
                                 payload={"e": "x"})
        r2 = td.degrade_to_local(s, delivery_key=dk, kind="agent_log",
                                 payload={"e": "x"})
        assert r1["degraded"] is True
        assert r2.get("idempotent_skip") is True and r2.get("degraded") is False
        n = (s.query(TraceLocalQueueItem)
             .filter(TraceLocalQueueItem.delivery_key == dk).count())
        assert n == 1          # 同键只排一队
    finally:
        _clean_queue(s, dk)
        s.close()


# ---- D4 降级本身失败也不抛（数据库故障）----

class _BrokenDB:
    """数据库故障替身：add/commit 抛错，rollback 可用。"""

    def add(self, *a, **k):
        raise RuntimeError("txn down")

    def rollback(self):
        pass

    def commit(self):
        raise RuntimeError("txn down")


def test_degrade_failure_best_effort_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise redis_py.ConnectionError("forced down")

    monkeypatch.setattr(rc, "xadd", _boom)
    # 主通道与降级队列双双故障：仍必须 best-effort 返回，绝不抛给主流程
    out = td.report_trace_record(_BrokenDB(), kind="agent_log",
                                 trace_id="t-broken", payload={"a": 1})
    assert out["reported"] is False
    assert out.get("degraded") is False
    assert "error" in out


# ---- D5 drain 重放：恢复后重放 OPEN -> DRAINED ----

def test_drain_replays_and_marks_drained():
    s = SessionTest()
    dk = _uniq("dk-d5")
    try:
        orig = rc.xadd

        def _boom(*a, **k):
            raise redis_py.ConnectionError("forced down")

        rc.xadd = _boom
        try:
            r = td.report_trace_record(s, kind="agent_log", trace_id=_uniq("t5"),
                                       payload={"event": "queued"})
            assert r["degraded"] is True
        finally:
            rc.xadd = orig

        # 通道恢复 -> drain 应重放成功（至少一次投递，采集侧凭 key 幂等）
        out = td.drain_local_queue(s, limit=200)
        assert out["drained"] >= 1
        row = (s.query(TraceLocalQueueItem)
               .filter(TraceLocalQueueItem.delivery_key == r["delivery_key"])
               .first())
        assert row is not None and row.status == "DRAINED"
        assert row.delivered_at is not None
    finally:
        _clean_queue(s, dk)
        s.close()


def test_drain_failures_accumulate_attempts():
    s = SessionTest()
    dk = _uniq("dk-d5b")
    try:
        orig = rc.xadd

        def _boom(*a, **k):
            raise redis_py.ConnectionError("still down")

        rc.xadd = _boom
        try:
            r = td.report_trace_record(s, kind="agent_log", trace_id=_uniq("t5b"),
                                       payload={"event": "nope"})
            assert r["degraded"] is True
            out = td.drain_local_queue(s, limit=50)
            assert out["failed"] >= 1
            row = (s.query(TraceLocalQueueItem)
                   .filter(TraceLocalQueueItem.delivery_key == r["delivery_key"])
                   .first())
            assert row.attempts >= 1 and row.status == TRACE_LQ_OPEN
        finally:
            rc.xadd = orig
    finally:
        _clean_queue(s, dk)
        s.close()


# ---- D6 graph 旁路写失败 -> 降级（不中断退款主流程）----

def test_graph_log_agent_degrades_on_db_write_failure(monkeypatch):
    import app.services.trace_degrade as TD
    import app.workflow.graph as G

    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return {"degraded": True}

    def _boom():
        raise RuntimeError("trace write down")

    monkeypatch.setattr(TD, "degrade_trace", _spy)
    monkeypatch.setattr(G, "SessionLocal", _boom)
    # 不抛异常（回归 O02 语义），且已触发降级入队
    G._log_agent("c-none", "ocr", "FAILED", {"note": "x"}, error="db down",
                 duration_ms=5)
    assert calls and calls[0]["kind"] == "agent_log"
    assert calls[0]["payload"]["error"] == "db down"
    assert calls[0]["payload"]["agent_name"] == "ocr"


# ---- HA 基础设施 ----

def test_redis_health_probe():
    h = rc.redis_health()
    assert h["ok"] is True and h["redis"] == "UP"


def test_safe_redis_failure_window(monkeypatch):
    calls: list[int] = []

    def _boom():
        calls.append(1)
        raise redis_py.ConnectionError("down")

    monkeypatch.setattr(rc, "_last_fail_ts", 0.0)
    assert rc.safe_redis(_boom, default="dflt") == "dflt"
    assert len(calls) == 1
    # 故障窗口内短路：不再发起调用
    assert rc.safe_redis(_boom, default="dflt") == "dflt"
    assert len(calls) == 1
    # 窗口过后（模拟超窗）重新探测
    monkeypatch.setattr(rc, "_last_fail_ts", time.monotonic() - 5.0)
    assert rc.safe_redis(_boom, default="dflt") == "dflt"
    assert len(calls) == 2


# ---- Readiness 就绪标记（独立扩缩容）----

def test_readiness_scale_out_and_down():
    from app.services import readiness as rd

    svc = "voice"
    try:
        i1 = _uniq("inst-v1")
        i2 = _uniq("inst-v2")
        # 相对计数断言：容忍 API 实例（api-<pid>）就绪心跳可能共存（30s TTL 内），
        # 只断言我加/减的两个实例产生了确定性增量——多实例扩缩容语义本身。
        base = next(x for x in rd.ready_snapshot()["services"]
                    if x["service"] == svc)["instances"]
        rd.mark_ready(svc, instance=i1)
        snap1 = next(x for x in rd.ready_snapshot()["services"]
                     if x["service"] == svc)
        assert snap1["ready"] is True and snap1["instances"] == base + 1
        rd.mark_ready(svc, instance=i2)                       # 第二实例并行就绪
        snap2 = next(x for x in rd.ready_snapshot()["services"]
                     if x["service"] == svc)
        assert snap2["instances"] == base + 2                 # 独立扩缩容：计数叠加
        rd.mark_not_ready(svc, instance=i1)                   # 缩容下线一个
        snap3 = next(x for x in rd.ready_snapshot()["services"]
                     if x["service"] == svc)
        assert snap3["instances"] == base + 1
    finally:
        rd.mark_not_ready(svc, instance=i2)
        rd.mark_not_ready(svc, instance=i1)


def test_readiness_unknown_service_rejected():
    from app.services import readiness as rd

    with pytest.raises(ValueError):
        rd.mark_ready("nope")


def test_health_ready_endpoint_aggregates(client):
    """启动钩子标记六大域就绪；/health/ready 聚合返回 overall + 每域实例数。"""
    r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"] is True
    assert len(body["services"]) == 6
    assert all(x["ready"] for x in body["services"])


def test_health_liveness_unchanged(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"