# -*- coding: utf-8 -*-
"""语音并发/容量/信号（V2.0 -5，计划 ）。

覆盖：
- 多路并发 WS 不串音/不串 Trace（各自 session 独立，finally 全部回收）；
- 容量超限拒绝（VoiceCapacityExceeded -> 429 capacity_full） + DROP 遥测；
- 断线重连计数（reconnect 信令/恢复端点）持久化 reconnects；
- 静音空闲超时收口（closed_reason=timeout）+ ERROR 遥测；
- 字幕/弱网/转人工信号（弱网落 weak_signal、转人工无任何退款副作用）；
- 恢复端点容量满 429（结构化 capacity_full body）。

红线：转人工/超时/并发 均不触碰退款——断言 case 数前后不变。
"""
import threading

import pytest

import app.services.voice_service as vs
import app.api.routes_voice as rv
from app.domain.models import RefundCase, VoiceMetric, VoiceSession
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _fresh_client():
    """每线程独立 TestClient（共享 app + get_db 覆盖），避免传输层竞争。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# ---------- 并发不串音/不串 Trace ----------

OTS = ["start", "stt", "tts", "reconnect", "ended"]


def _worker(fresh_client, i, url, bucket):
    try:
        events = []
        c = fresh_client()
        with c.websocket_connect(url) as ws:
            ev = ws.receive_json()
            assert ev["type"] == "start"
            events.append(ev)
            ws.send_json({"type": "stt",
                          "text": f"订单 ORD-C-{i}{i} 金额 12 元 我要退款"})
            events.append(ws.receive_json())          # stt
            events.append(ws.receive_json())          # tts
            ws.send_json({"type": "reconnect"})
            events.append(ws.receive_json())          # reconnect
            ws.send_json({"type": "stop"})
            events.append(ws.receive_json())          # ended
        bucket[i] = events
    except Exception as exc:  # noqa: BLE001 —— 交给主线程断言暴露
        bucket[i] = exc


def test_voice_concurrent_no_crosstalk_or_trace(client, monkeypatch):
    """6 路并发 WS：每线程事件序列一致且 session 独立（不串音/不串 Trace），
    结束后 SessionManager 全部回收。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=20))
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"
    bucket = {}
    threads = [threading.Thread(
        target=_worker, args=(_fresh_client, i, url, bucket)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(6):
        evs = bucket[i]
        assert not isinstance(evs, Exception), f"thread {i} failed: {evs}"
        assert [e["type"] for e in evs] == OTS, f"thread {i} seq mismatch"
        sids = {e["session_id"] for e in evs}
        assert len(sids) == 1, "同一会话 Trace session_id 必须一致（不串 Trace）"

    all_sids = [bucket[i][0]["session_id"] for i in range(6)]
    assert len(set(all_sids)) == 6, "6 路会话 session_id 必须两两独立（不串音）"
    assert vs.voice_manager.active_count == 0, "结束后注册表应全部回收"

    # 落库行 reconnects=1（reconnect 信令已持久化）
    s = SessionTest()
    try:
        rows = (s.query(VoiceSession)
                .filter(VoiceSession.session_id.in_(all_sids))
                .filter_by(reconnects=1).count())
        assert rows == 6, f"应 6 行 reconnects=1，实际 {rows}"
    finally:
        s.close()


# ---------- 容量超限 ----------

def test_voice_capacity_full_rejects_and_drop(client, monkeypatch):
    """排队已满（V3-5）：第二个连接回 capacity_full 并 429 关闭 + DROP 遥测落库。

    V3-5 语义：容量超限不再直接 429，而是**排队**；queue_capacity=0 模拟排队已满
    （系统过载）-> 立即 capacity_full + DROP。默认排队/排队超时路径见
    test_voice_queue_fallback.py。"""
    import starlette.websockets as ws_mod

    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=1, queue_capacity=0))
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"

    s = SessionTest()
    base_drop = s.query(VoiceMetric).filter_by(metric_kind="DROP").count()
    s.close()

    with client.websocket_connect(url) as ws1:
        ev = ws1.receive_json()
        assert ev["type"] == "start"

        with client.websocket_connect(url) as ws2:
            ev2 = ws2.receive_json()
            assert ev2["type"] == "error"
            assert ev2["code"] == "capacity_full"
            assert ev2["max_concurrent"] == 1
            # close(429) 在 TestClient 传回既可能是异常也可能是 disconnect 帧
            try:
                close_msg = ws2.receive()
                assert isinstance(close_msg, dict)
                assert close_msg.get("type") in ("websocket.disconnect",
                                                 "websocket.close")
            except ws_mod.WebSocketDisconnect as exc:
                assert exc.code in (429, 1000)

        # ws1 仍可用（并发正确性：被拒会话不影响已占会话）
        ws1.send_json({"type": "stop"})
        ev = ws1.receive_json()
        assert ev["type"] == "ended"

    s = SessionTest()
    drops = s.query(VoiceMetric).filter_by(metric_kind="DROP").count()
    assert drops > base_drop, "容量拒绝应落 DROP 遥测"
    s.close()


def test_voice_capacity_recovery_endpoint_429(client, monkeypatch):
    """恢复端点容量满（结构化 body capacity_full），CS 语义完好。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=2))
    token = _auth(client)
    h = {"Authorization": f"Bearer {token}"}
    # 先建 2 路灌满
    for _ in range(2):
        vs.voice_manager.create()
    r = client.post("/api/v1/voice/ws/recovery",
                    data={"session_id": "vs-full-here"}, headers=h)
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "capacity_full"
    assert r.json()["active"] == 2 and r.json()["max_concurrent"] == 2


# ---------- 断线重连持久化 ----------

def test_voice_reconnect_endpoint_increments_and_persists(client, monkeypatch):
    mo = monkeypatch
    mo.setattr(vs, "voice_manager", vs.VoiceSessionManager(max_concurrent=8))
    token = _auth(client)
    h = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/v1/voice/ws/recovery",
                    data={"session_id": "vs-rec-abc"}, headers=h)
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = client.post("/api/v1/voice/ws/recovery", data={"session_id": sid}, headers=h)
    assert r.json()["reconnects"] == 1
    r = client.get(f"/api/v1/voice/session/{sid}", headers=h)
    assert r.json()["reconnects"] == 1
    # 回收后 404（审计走记录表）
    vs.voice_manager.drop(sid)
    r = client.get(f"/api/v1/voice/session/{sid}", headers=h)
    assert r.status_code == 404


def test_voice_reconnect_signal_inside_ws(client, monkeypatch):
    """WS 内 reconnect 信令：reconnects 递增 1 -> 2，落库行一致。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=4))
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"
    with client.websocket_connect(url) as ws:
        ws.receive_json()                               # start
        ws.send_json({"type": "stt", "text": "我要退款"})
        ws.receive_json()                               # stt
        tts = ws.receive_json()                         # tts
        sid = tts["session_id"]
        ws.send_json({"type": "reconnect"})
        ev = ws.receive_json()
        assert ev["type"] == "reconnect"
        assert "reconnects=1" in (ev["reply"] or "")
        ws.send_json({"type": "reconnect"})
        ev = ws.receive_json()
        assert "reconnects=2" in (ev["reply"] or "")
        ws.send_json({"type": "stop"})
        ws.receive_json()
    s = SessionTest()
    try:
        row = (s.query(VoiceSession)
               .filter_by(session_id=sid).order_by(VoiceSession.created_at.desc())
               .first())
        assert row is not None and row.reconnects == 2
    finally:
        s.close()


# ---------- 静音超时 / 信号（字幕/弱网/转人工） ----------

def test_voice_silence_timeout_closes_with_error(client, monkeypatch):
    """静默超时 -> timeout 事件 + closed_reason=timeout + ERROR 遥测，无退款。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=4))
    monkeypatch.setattr(rv, "_WS_IDLE_TIMEOUT", 0.05)
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"

    s = SessionTest()
    base_err = s.query(VoiceMetric).filter_by(metric_kind="ERROR").count()
    base_case = s.query(RefundCase).count()
    s.close()

    with client.websocket_connect(url) as ws:
        ev = ws.receive_json()
        assert ev["type"] == "start"
        # 静默挂起 > 0.05s（不做任何发送）-> timeout
        ev = ws.receive_json()
        assert ev["type"] == "timeout"

    s = SessionTest()
    try:
        errs = s.query(VoiceMetric).filter_by(metric_kind="ERROR").count()
        assert errs > base_err, "超时应落 ERROR 遥测"
        row = (s.query(VoiceSession)
               .filter_by(closed_reason="timeout")
               .order_by(VoiceSession.created_at.desc()).first())
        assert row is not None, "超时会话应落 closed_reason=timeout"
        assert s.query(RefundCase).count() == base_case, "超时不得触发退款"
    finally:
        s.close()


def test_voice_handoff_weak_caption_no_side_effect(client, monkeypatch):
    """字幕/弱网/转人工信号：state=HUMAN_HANDOFF、weak_signal=1，
    转人工前后退款案件数不变（无副作用红线）。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=4))
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"

    s = SessionTest()
    base_case = s.query(RefundCase).count()
    s.close()

    with client.websocket_connect(url) as ws:
        ev = ws.receive_json()
        assert ev["type"] == "start"
        ws.send_json({"type": "caption", "text": "先生您好"})
        ev = ws.receive_json()
        assert ev["type"] == "caption" and ev["text"] == "先生您好"
        ws.send_json({"type": "weak"})
        ev = ws.receive_json()
        assert ev["type"] == "weak_signal"
        assert ev["state"] in ("LISTENING", "STT", "LLM", "TTS", "INTERRUPTED")
        ws.send_json({"type": "human_handoff", "reason": "客户要求人工"})
        ev = ws.receive_json()
        assert ev["type"] == "human_handoff"
        assert ev["state"] == "HUMAN_HANDOFF"
        assert "已转人工" in (ev["reply"] or "")

    s = SessionTest()
    try:
        row = (s.query(VoiceSession)
               .filter_by(closed_reason="handoff")
               .order_by(VoiceSession.created_at.desc()).first())
        assert row is not None
        assert row.weak_signal is True, "弱网标记应持久化 weak_signal=1"
        assert s.query(RefundCase).count() == base_case, "转人工不得触发退款"
    finally:
        s.close()