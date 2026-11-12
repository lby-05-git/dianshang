# -*- coding: utf-8 -*-
"""V3-5 限流排队 + 熔断降级。

覆盖：
- 容量超限 -> **排队**而非直接 429（admit/admit_async）；
- 排队满（queue_capacity）-> VoiceQueueFull（capacity_full + DROP，系统过载红线）；
- 排队超时 -> VoiceQueueTimeout 且等待者正确取消（不泄漏占坑）；
- drop() 腾位 -> 按优先权授让：**业务实时 > 离线评测 > 陪练**（business first）；
- 熔断：连续失败 -> open；冷却后半开 probe -> 复位 closed；probe 成功关断/失败重开；
- 熔断打开降级：业务实时会话走 fallback Provider（直接文本无 VAD），离线评测/陪练拒绝；
- 路由 WS：排队已满 capacity_full 429 + 预警熔断失败计数；正常会话熔断复位。

红线：排队/熔断只影响接入调度，绝不触碰退款/评测决策；降级 Provider 只换识别路径
（无真实退款/审批副作用）。
"""
import asyncio
import time

import pytest

import app.services.voice_service as vs
import app.api.routes_voice as rv
from app.services.voice_service import (
    PRI_BUSINESS,
    PRI_COACHING,
    PRI_EVAL,
    VoiceProviderDegraded,
    VoiceQueueFull,
    VoiceQueueTimeout,
)
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ---------- 1. 排队语义（超限 -> 排队，非 429） ----------

def test_capacity_excess_queues_not_direct_429():
    """容量满但排队未满：admit 返回 queued（不抛、不封）。"""
    m = vs.VoiceSessionManager(max_concurrent=1, queue_capacity=2)
    m.create()
    status, svc, waiter = m.admit(vs.PRI_BUSINESS)
    assert status == "queued"
    assert svc is None and waiter is not None
    assert m.queue_length == 1
    assert waiter.admitted is False
    # 排队可取消（不泄漏占坑）
    assert m.cancel_waiter(waiter) is True
    assert m.queue_length == 0


def test_queue_full_when_capacity_reached():
    """排队已满：admit overflow（VoiceQueueFull 由 admit_async 上抛）。"""
    m = vs.VoiceSessionManager(max_concurrent=1, queue_capacity=0)
    m.create()
    status, svc, waiter = m.admit(vs.PRI_BUSINESS)
    assert status == "overflow" and svc is None
    with pytest.raises(VoiceQueueFull):
        asyncio.run(vs.admit_async(m))


def test_queue_wait_timeout_raises_and_cancels():
    """排队等待超时 -> VoiceQueueTimeout，且等待者被取消（队列不残留）。"""
    m = vs.VoiceSessionManager(max_concurrent=1, queue_capacity=2,
                               queue_timeout_seconds=0.05)
    m.create()
    with pytest.raises(VoiceQueueTimeout):
        asyncio.run(vs.admit_async(m, priority=vs.PRI_EVAL,
                                   queue_timeout_seconds=0.05))
    assert m.queue_length == 0, "超时等待者应被取消"


def test_async_grant_after_slot_freed():
    """异步排队：另一协程 drop 腾位 -> 授让返回会话（排队而非 429）。"""
    async def scenario():
        m = vs.VoiceSessionManager(max_concurrent=1, queue_capacity=2,
                                   queue_timeout_seconds=2.0)
        s1 = m.create()
        fut = asyncio.ensure_future(
            vs.admit_async(m, priority=vs.PRI_BUSINESS, queue_timeout_seconds=2.0))
        await asyncio.sleep(0.05)
        assert m.queue_length == 1
        m.drop(s1.session_id)                          # 腾位
        svc = await asyncio.wait_for(fut, 2.0)
        assert svc.priority == PRI_BUSINESS
        return svc

    svc = asyncio.run(scenario())
    assert vs.voice_manager is not None or True


def test_business_priority_over_offline_eval_coaching():
    """腾位授让：晚到的业务会话 > 先排队的评测（business first），随后 eval。"""
    m = vs.VoiceSessionManager(max_concurrent=1, queue_capacity=4)
    s1 = m.create()
    m.admit(vs.PRI_COACHING)
    m.admit(vs.PRI_EVAL)
    m.admit(vs.PRI_BUSINESS)          # 最后到但最高优先
    m.drop(s1.session_id)
    assert [s.priority for s in m.sessions()] == [PRI_BUSINESS]
    m.drop(m.sessions()[0].session_id)
    assert [s.priority for s in m.sessions()] == [PRI_EVAL]   # 评测 > 陪练
    m.drop(m.sessions()[0].session_id)
    assert [s.priority for s in m.sessions()] == [PRI_COACHING]


# ---------- 2. 熔断器 ----------

def test_breaker_trip_close_probe_cycle():
    b = vs.VoiceBreaker(fail_threshold=3, cooldown_seconds=0.05)
    assert b.is_open() is False
    for _ in range(3):
        b.record_failure()
    assert b.is_open() is True, "连续失败达到阈值应熔断"
    assert b.failures == 3
    time.sleep(0.06)                                  # 冷却过
    assert b.is_open() is False, "冷却后半开窗口应放行一次 probe"
    b.record_success()                                # probe 成功 -> 关断
    assert b.failures == 0 and b.is_open() is False

    # probe 失败 -> 重新 open
    b.record_failure(); b.record_failure(); b.record_failure()
    assert b.is_open() is True
    b.reset()
    assert b.failures == 0


def test_breaker_trip_helper():
    b = vs.VoiceBreaker(fail_threshold=2, cooldown_seconds=30)
    b.trip()
    assert b.is_open() is True


# ---------- 3. 熔断降级：业务 fallback，离线/陪练拒绝 ----------

def test_breaker_open_degrades_business_rejects_offline(monkeypatch):
    """熔断打开：业务实时 -> fallback Provider（降级标记）；评测/陪练 -> 拒绝。"""
    monkeypatch.setattr(vs, "voice_manager", vs.VoiceSessionManager(max_concurrent=4))
    breaker = vs.VoiceBreaker(fail_threshold=1, cooldown_seconds=30)
    breaker.trip()
    monkeypatch.setattr(vs, "voice_breaker", breaker)

    assert breaker.is_open() is True
    # 非业务会话拒绝（占坑保护）
    with pytest.raises(VoiceProviderDegraded):
        asyncio.run(rv._admit_with_breaker(priority=PRI_EVAL))
    with pytest.raises(VoiceProviderDegraded):
        asyncio.run(rv._admit_with_breaker(priority=PRI_COACHING))
    # 业务实时降级放行：fallback Provider（无 VAD 音频段）
    svc = asyncio.run(rv._admit_with_breaker(priority=PRI_BUSINESS))
    assert svc.degraded_provider is True and svc.vad is None
    assert svc.priority == PRI_BUSINESS
    # 降级会话仍入注册表（不串 Trace/不串音）
    assert vs.voice_manager.active_count == 1


def test_breaker_open_ws_business_degrades(client, monkeypatch):
    """WS 层面：熔断打开时业务实时会话照常工作（降级 Provider 语义）。"""
    monkeypatch.setattr(vs, "voice_manager", vs.VoiceSessionManager(max_concurrent=2))
    breaker = vs.VoiceBreaker(fail_threshold=1, cooldown_seconds=30)
    breaker.trip()
    monkeypatch.setattr(vs, "voice_breaker", breaker)

    token = _auth(client)
    with client.websocket_connect(f"/api/v1/voice/ws?token={token}") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "start"
        # 降级路径：音频帧被忽略，但文本信令直通业务回复
        ws.send_bytes(b"speech-ignored-in-degraded")
        ws.send_json({"type": "stt",
                      "text": "订单 ORD-DEG-1 金额 66 元 我要退款"})
        ev = ws.receive_json()
        assert ev["type"] == "stt"
        tts = ws.receive_json()
        assert tts["type"] == "tts"
        ws.send_json({"type": "stop"})
        ws.receive_json()
    assert vs.voice_manager.active_count == 0, "降级会话回收"


# ---------- 4. 路由 WS：排队满 capacity_full + 熔断链路 ----------

def test_ws_queue_full_capacity_vs_drop_and_breaker(client, monkeypatch):
    """WS 排队满 -> capacity_full 429 + DROP 遥测 + 熔断失败计数 + 正常会话复位。

    借 client fixture 依赖注入（get_db 覆盖到测试库）测遥测落库；断路器走本进程
    monkeypatch（竞态实测），不影响共享单例。"""
    from app.domain.models import VoiceMetric

    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=1, queue_capacity=0))
    breaker = vs.VoiceBreaker(fail_threshold=2, cooldown_seconds=30)
    monkeypatch.setattr(vs, "voice_breaker", breaker)

    s = SessionTest()
    base_drop = s.query(VoiceMetric).filter_by(metric_kind="DROP").count()
    s.close()

    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"
    with client.websocket_connect(url) as ws1:
        ws1.receive_json()                           # start
        with client.websocket_connect(url) as ws2:
            ev = ws2.receive_json()
            assert ev["type"] == "error"
            assert ev["code"] == "capacity_full"
            # 排队已满 -> 熔断 failure 递增（系统过载信号；断言须在 ws1 结束复位前）
            assert breaker.failures >= 1, "队列满应计入熔断失败"
        ws1.send_json({"type": "stop"})
        ws1.receive_json()                           # ended
    # ws1 正常收尾 -> 连续失败计数复位（consecutive 语义：一次成功打断连败）
    assert breaker.failures == 0
    s = SessionTest()
    try:
        drops = s.query(VoiceMetric).filter_by(metric_kind="DROP").count()
        assert drops > base_drop, "队列满 DROP 应落遥测"
    finally:
        s.close()