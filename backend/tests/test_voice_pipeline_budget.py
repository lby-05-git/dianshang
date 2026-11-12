# -*- coding: utf-8 -*-
"""V3-5 链路时延预算。

流水线分段预算（Mock 实测口径，诚实未测得=None）：
  VAD 断点 ≤150ms / STT ≤200ms / LLM 首Token ≤300ms / TTS 首包 ≤200ms。

覆盖：
- 会话级 pipeline_report：主 Provider 全链路 4 段达标 + 端到端首响应口径；
- 降级 Provider（fallback）无 VAD：vad_end 如实 None，绝不编造数值；
- 超预算分支诚实 False（注入分段值 > 预算判定）；
- 遥测新增 4 种 metric_kind（VAD_END/STT/LLM_FIRST/TTS_FIRST）可落库 + 非法拒绝；
- P95 / 预算自洽断言（共享库只做结构/自洽，不跨测试精确比对）；
- WS 集成：真实会话 4 段分段实测入库且不超预算；//voice/metrics 呈 4 段预算。

红线：只写观测，绝不触碰退款/评测决策/会话业务内容。
"""
import uuid

import pytest

from app.core.config import settings
from app.domain.models import VoiceMetric
from app.services import voice_service as vs
from app.services import voice_telemetry as vt
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login",
                       json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ---------- 1. 会话级预算报告 ----------

def test_pipeline_report_all_segments_meet_budget():
    """主 Provider 全流程：VAD≤150 / STT≤200 / LLM首Token≤300 / TTS首包≤200 全达标。"""
    svc = vs.VoiceSessionService()
    svc.on_start()
    svc.on_audio(b"speech-frame-128")            # 开启语音
    for _ in range(30):                           # 1.5s 静音 -> vad_end
        svc.on_audio(b"\x00" * 800)
    svc.on_stt("订单 ORD-20260901 金额 128 元 我要退款")
    svc.on_tts()

    r = svc.pipeline_report()
    assert r["degraded_provider"] is False
    for key, expected_ms in (("vad_end", settings.VOICE_VAD_BUDGET_MS),
                             ("stt", settings.VOICE_STT_BUDGET_MS),
                             ("llm_first", settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS),
                             ("tts_first", settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS)):
        seg = r["segments"][key]
        assert seg["seconds"] is not None, f"{key} 主链路应测得"
        assert seg["budget_ms"] == expected_ms
        assert seg["meets"] is True, f"{key} 应 ≤ {expected_ms}ms（Mock 同步管道实测）"
    assert r["end_to_end_first_response_seconds"] is not None


def test_pipeline_report_fallback_vad_honest_none():
    """降级 Provider：无 VAD 音频段 -> vad_end 如实 None（不编造数值）。"""
    f = vs.VoiceSessionService(provider="fallback", priority=vs.PRI_BUSINESS)
    assert f.vad is None and f.degraded_provider is True
    assert f.on_audio(b"x-frame") is None         # 降级忽略音频帧
    f.on_stt("订单 ORD-999 金额 10 元 我要退款")
    f.on_tts()
    r = f.pipeline_report()
    assert r["degraded_provider"] is True
    assert r["segments"]["vad_end"]["seconds"] is None
    assert r["segments"]["vad_end"]["meets"] is False   # 未测得绝不通过
    assert r["segments"]["stt"]["meets"] is True


def test_pipeline_report_budget_overrun_honest_false():
    """注入超预算分段：meets 如实 False（预算比较逻辑确定性验证）。"""
    svc = vs.VoiceSessionService()
    svc.on_stt("hello")
    svc.on_tts()
    svc.segments["stt"] = 5.0                      # 模拟 5s（>200ms）STT
    r = svc.pipeline_report()
    assert r["segments"]["stt"]["meets"] is False
    assert r["segments"]["stt"]["seconds"] == 5.0
    assert r["segments"]["tts_first"]["meets"] is True


def test_config_budget_constants():
    """预算常量与计划一致：150/200/300/200ms。"""
    assert settings.VOICE_VAD_BUDGET_MS == 150
    assert settings.VOICE_STT_BUDGET_MS == 200
    assert settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS == 300
    assert settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS == 200
    assert 30 <= settings.VOICE_MAX_CONCURRENT <= 40     # V3-5 30-40 路


# ---------- 2. 遥测分段指标 ----------

def test_segment_metric_kinds_registered_and_reject_invalid():
    """VAD_END/STT/LLM_FIRST/TTS_FIRST 可落库；非法 kind 拒绝（只写观测）。"""
    s = SessionTest()
    try:
        for kind in (vt.METRIC_VAD_END, vt.METRIC_STT,
                     vt.METRIC_LLM_FIRST, vt.METRIC_TTS_FIRST):
            row = vt.record_metric(s, session_id="seg-1",
                                   metric_kind=kind, value=0.05)
            assert row.metric_kind == kind
        with pytest.raises(ValueError):
            vt.record_metric(s, session_id="seg-x",
                             metric_kind="NOT_A_KIND", value=1.0)
        # record_pipeline_metrics：主会话 4 段一次落库
        svc = vs.VoiceSessionService()
        svc.on_stt("订单 ORD-1 金额 1 元 我要退款")
        svc.on_tts()
        rows = vt.record_pipeline_metrics(
            s, svc)                  # stt/llm/tts 三段（无音频故无 vad_end）
        kinds = {r.metric_kind for r in rows}
        assert vt.METRIC_STT in kinds and vt.METRIC_TTS_FIRST in kinds
    finally:
        s.close()


def test_p95_and_budget_self_consistent():
    """compute_baseline.pipeline_budget：4 分段键齐 + budget_ms 与预算常量一致
    + meets_budget 与 p95<=预算 自洽（共享库不跨测试精确比对）。"""
    s = SessionTest()
    try:
        b = vt.compute_baseline(s, window_minutes=60)
        seg = b["pipeline_budget"]
        assert set(seg) == {"VAD_END", "STT", "LLM_FIRST", "TTS_FIRST"}
        expect = {
            "VAD_END": settings.VOICE_VAD_BUDGET_MS,
            "STT": settings.VOICE_STT_BUDGET_MS,
            "LLM_FIRST": settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS,
            "TTS_FIRST": settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS,
        }
        for kind, budget_ms in expect.items():
            d = seg[kind]
            assert d["budget_ms"] == budget_ms
            p95 = d["p95_seconds"]
            assert d["meets_budget"] == (p95 is not None
                                         and p95 <= budget_ms / 1000.0)
        assert vt._p95([0.1, 0.2, 0.3, 0.4]) == 0.4   # idx=int(0.95*4)=3
        assert vt._p95([0.4]) == 0.4
        assert vt._p95([]) == 0.0
    finally:
        s.close()


# ---------- 3. WS 集成：分段实测入库 ----------

def test_ws_session_records_pipeline_segments_within_budget(client, monkeypatch):
    """真实 WS 会话：VAD_END/STT/LLM_FIRST/TTS_FIRST 实测入库且 ≤ 各自预算。"""
    monkeypatch.setattr(vs, "voice_manager",
                        vs.VoiceSessionManager(max_concurrent=4))
    token = _auth(client)
    url = f"/api/v1/voice/ws?token={token}"
    with client.websocket_connect(url) as ws:
        ws.receive_json()                               # start
        ws.send_bytes(b"speech-128-yuan")                # 开说
        for _ in range(30):
            ws.send_bytes(b"\x00" * 800)                 # 1.5s 静音 -> vad_end
        ws.receive_json()                                # vad_end
        ws.send_json({"type": "stt",
                      "text": "订单 ORD-BUDGET-77 金额 128 元 我要退款"})
        ws.receive_json()                                # stt
        tts = ws.receive_json()                          # tts
        sid = tts["session_id"]
        ws.send_json({"type": "stop"})
        ws.receive_json()                                # ended

    s = SessionTest()
    try:
        budget = {
            vt.METRIC_VAD_END: settings.VOICE_VAD_BUDGET_MS / 1000.0,
            vt.METRIC_STT: settings.VOICE_STT_BUDGET_MS / 1000.0,
            vt.METRIC_LLM_FIRST: settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS / 1000.0,
            vt.METRIC_TTS_FIRST: settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS / 1000.0,
        }
        for kind, budget_sec in budget.items():
            rows = (s.query(VoiceMetric)
                    .filter_by(session_id=sid, metric_kind=kind).all())
            assert rows, f"会话应落 {kind} 分段遥测"
            assert rows[0].value <= budget_sec, \
                f"{kind} 实测 {rows[0].value}s 应 ≤ {budget_sec}s"
    finally:
        s.close()


def test_voice_metrics_endpoint_has_pipeline_budget(client):
    """/voice/metrics（主管）返回 pipeline_budget 4 分段结构。"""
    sup = _auth(client, "supervisor1")
    cs = _auth(client, "customer_service1")
    assert client.get("/api/v1/voice/metrics",
                      headers={"Authorization": f"Bearer {cs}"}).status_code == 403
    r = client.get("/api/v1/voice/metrics",
                   headers={"Authorization": f"Bearer {sup}"})
    assert r.status_code == 200, r.text
    assert set(r.json()["pipeline_budget"]) == {"VAD_END", "STT",
                                                "LLM_FIRST", "TTS_FIRST"}