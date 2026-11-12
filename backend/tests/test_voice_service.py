# -*- coding: utf-8 -*-
"""语音会话服务单测（基线 V1.0，对齐实施计划 ）。

覆盖：
- MockVAD：1.5s 静音断点触发 vad_end；无静音不触发；
- VoiceSessionService：状态机 LISTENING->STT->LLM->TTS + interrupt 插话清 TTS 队列 + 会话结束回收；
- MockSTT：订单号/金额提取；
- 回调：interrupt 触发 tts_cleared+=1（插话停止播报指标）。
"""
import time

from app.services.voice_service import (
    SIG_INTERRUPT,
    VOICE_ENDED,
    VOICE_INTERRUPTED,
    VOICE_LISTENING,
    VOICE_LLM,
    VOICE_STT,
    VOICE_TTS,
    MockSTT,
    MockVAD,
    VoiceSessionService,
)


def _silence_frames(ms: int, frame_interval_ms: int = 50) -> list[bytes]:
    """构造一段静音帧序列（字节全零，模拟无语音）。"""
    n = max(1, ms // frame_interval_ms)
    empty = b"\x00" * 800  # 空音频（模拟静音）
    return [empty] * n


def test_vad_breakpoint_on_1_5s_silence():
    """1.5s 静音 -> vad_end（MockVAD 断点）。"""
    vad = MockVAD(silence_ms=1500)
    vad.feed(b"audio-frame-1")        # 开始说话
    assert vad.feed(b"\x00" * 800) is False   # 开始静音
    # 到 1.5s 边界（模拟 50ms 粒度推进，需 30 静音帧）
    frames = _silence_frames(1600, frame_interval_ms=50)
    tripped = False
    for fr in frames:
        if vad.feed(fr):
            tripped = True
            break
    assert tripped, "达到 1.5s 静音应触发断点"


def test_vad_no_breakpoint_without_silence():
    """持续有语音 -> 不触发断点。"""
    vad = MockVAD(silence_ms=1500)
    for _ in range(30):
        assert vad.feed(b"speech" + bytes([_ % 256])) is False


def test_voice_session_state_machine_and_response():
    """状态机：LISTENING -> STT -> LLM -> TTS；STT 含订单号/金额 -> 确认文案。"""
    svc = VoiceSessionService()
    assert svc.state == VOICE_LISTENING

    start = svc.on_start()
    assert start.state == VOICE_LISTENING

    # VAD 断点（模拟 1.5s 静音后的语音段结束）
    vad_event = svc.on_audio(b"audio-buffer")
    assert vad_event is None
    for fr in _silence_frames(1600):
        ev = svc.on_audio(fr)
        if ev is not None:
            assert ev.type == "vad_end"
    # on_audio 完成断点后状态置 STT（等待识别）
    assert svc.state == VOICE_STT

    # STT：识别文本
    stt = svc.on_stt("我要退款，订单 ORD-20260901，金额 128 元")
    assert stt.type == "stt"
    assert stt.state == VOICE_LLM
    assert "ORD-20260901" in (stt.reply or ""), f"应识别订单号: {stt.reply}"
    # 金额被业务侧掩码（128 -> 100+ 防侧录），回复里不含原始小数/完整数字
    assert "100" in (stt.reply or "")
    assert "128" not in (stt.reply or "")

    # TTS 播报
    tts = svc.on_tts()
    assert tts.type == "tts"
    assert tts.state == VOICE_TTS


def test_interrupt_clears_tts_queue_and_metric():
    """interrupt：清空 TTS 队列 + tts_cleared+=1 + 状态置 INTERRUPTED。"""
    svc = VoiceSessionService()
    svc.on_stt("我要退款，订单 ORD-12345，金额 128 元")
    svc.on_tts()  # TTS 入队

    event = svc.on_interrupt()
    assert event.type == SIG_INTERRUPT
    assert event.state == VOICE_INTERRUPTED
    assert svc.state == VOICE_INTERRUPTED
    assert svc.tts_cleared == 1
    assert svc.tts.queue == []  # 队列已清空（物理打断指标）


def test_stop_ends_session():
    """stop -> ENDED + 回收。"""
    svc = VoiceSessionService()
    svc.on_stt("我要退款")
    event = svc.on_stop()
    assert event.type == "ended"
    assert event.state == VOICE_ENDED


def test_stt_extracts_order_and_amount():
    """MockSTT：提取订单号与金额；无关键信息给兜底。"""
    stt = MockSTT()
    r = stt.transcribe("订单号 ORD-AABB11，退款金额 99 元")
    assert "ORD-AABB11" in (r["order_id"] or "")
    assert r["amount"] == 99.0

    # 无关键信息兜底
    r2 = stt.transcribe("你好，我想问问情况")
    assert r2["order_id"] is None
    assert r2["amount"] is None