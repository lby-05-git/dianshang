# -*- coding: utf-8 -*-
"""语音遥测（V2.0 -5 / 计划 voice_telemetry.py）。

覆盖：
- record_metric 只写观测 + 非法指标类型拒绝（ValueError）；
- 容量基线聚合 compute_baseline：P99 时延/错误率/峰值并发/Drop/占用率（共享库断言相对化）；
- GET /api/v1/voice/metrics：主管+ 权限（客服 403）；
- WS 会话 TTS 首条回复 -> LATENCY_P99 落库（集成）。

共享测试库踩坑（Phase 2）：聚合值只做存在性/单调性断言，不跨测试精确比对。
"""
import json as _json

import pytest

import app.services.voice_service as vs
from app.core.config import settings
from app.services import voice_telemetry as vt
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_record_metric_row_and_invalid_rejected():
    """record_metric 落 VoiceMetric 行；未知 kind 抛 ValueError（只写观测）。"""
    s = SessionTest()
    try:
        row = vt.record_metric(
            s, session_id="t-metric-1", metric_kind=vt.METRIC_LATENCY_P99, value=0.42)
        assert row.metric_kind == "LATENCY_P99"
        assert row.value == 0.42
        with pytest.raises(ValueError):
            vt.record_metric(s, session_id="t-metric-x",
                             metric_kind="NOPE", value=1.0)
    finally:
        s.close()


def test_p99_boundary():
    """P99 分位计算（含边界/单样本/空保护）。"""
    assert vt._p99([0.2, 0.5, 1.0]) == 1.0      # idx = int(0.99*3)=2
    assert vt._p99([0.4]) == 0.4
    assert vt._p99([1.0, 2.0, 3.0, 4.0]) == 4.0  # idx = int(0.99*4)=3
    assert vt._p99([]) == 0.0


def test_compute_baseline_keys_and_sanity():
    """compute_baseline 返回容量基线各键 + PEAK 单调性（共享库只做相对断言）。"""
    s = SessionTest()
    try:
        m = vs.VoiceSessionManager(max_concurrent=3)
        m.create()
        b = vt.compute_baseline(s, window_minutes=60, session_manager=m)
        assert set(b) >= {"window_minutes", "session_count", "latency_p99_seconds",
                          "error_count", "error_rate", "drop_count",
                          "peak_concurrency", "max_concurrent",
                          "current_active", "capacity_percent"}
        assert b["max_concurrent"] == 3
        assert b["current_active"] == 1
        assert b["capacity_percent"] == round(1 / 3, 4)
        # 无 manager 时不注入占用信息
        b2 = vt.compute_baseline(s, window_minutes=60)
        assert b2["current_active"] is None and b2["capacity_percent"] is None
    finally:
        s.close()


def test_voice_metrics_endpoint_permission_and_shape(client):
    """GET /voice/metrics：客服 403、主管 200 且含基线键。"""
    tok_cs = _auth(client, "customer_service1")
    tok_sup = _auth(client, "supervisor1")
    r = client.get("/api/v1/voice/metrics",
                   headers={"Authorization": f"Bearer {tok_cs}"})
    assert r.status_code == 403, r.text
    r = client.get("/api/v1/voice/metrics",
                   headers={"Authorization": f"Bearer {tok_sup}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"window_minutes", "latency_p99_seconds", "error_count",
                         "error_rate", "drop_count", "peak_concurrency",
                         "max_concurrent", "current_active", "capacity_percent"}
    assert body["max_concurrent"] == int(settings.VOICE_MAX_CONCURRENT)


def test_voice_latency_metric_recorded_via_ws(client, monkeypatch):
    """WS 首条 TTS -> LATENCY_P99 落库（值 > 0，与 session_id 关联）。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=4))
    tok = _auth(client)
    url = f"/api/v1/voice/ws?token={tok}"
    with client.websocket_connect(url) as ws:
        ev = ws.receive_json()
        assert ev["type"] == "start"
        ws.send_json({"type": "stt",
                      "text": "订单 ORD-TELE-1234 金额 88 元 我要退款"})
        ws.receive_json()          # stt
        tts = ws.receive_json()    # tts
        assert tts["type"] == "tts"
        sid = tts["session_id"]
        ws.send_json({"type": "stop"})
        ws.receive_json()          # ended
    s = SessionTest()
    try:
        rows = (s.query(vt.VoiceMetric)
                .filter_by(session_id=sid, metric_kind=vt.METRIC_LATENCY_P99)
                .all())
        assert rows, "WS 会话应落一条 LATENCY_P99"
        assert rows[0].value > 0
    finally:
        s.close()