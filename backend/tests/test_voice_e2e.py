# -*- coding: utf-8 -*-
"""语音 + 陪练集成测试（基线 V1.0，对齐实施计划 ）。

覆盖：
- WS /api/v1/voice/ws：Mock 音频帧 -> VAD 切分 -> stt -> tts 播报态 + interrupt 打断清队列；
- POST /api/v1/voice/create-test-case：语音识别关键信息 -> 创建**测试工单**（EVAL 打标），
  且**无真实退款**（不启动图；断言无 RefundTransaction）；
- POST /api/v1/voice/coaching/start + score：1 场景 5 维评分，只读建议无副作用。
"""
import json as _json

import pytest

from app.domain.models import CoachingSession, RefundCase, RefundTransaction, VoiceSession
from tests.conftest import SessionTest


def _auth(client, username="customer_service1"):
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_voice_ws_flow_and_interrupt(client):
    """WS 全流程：start -> VAD 断点(STT) -> stt -> tts -> interrupt 清队列。

    锁步协议：每条控制输入产生已知数量的事件，逐一对齐接收（避免空等挂死）。
      start       -> 1 事件（start）
      41 音频帧   -> 1 事件（vad_end，仅第 30 个静音帧触发）
      stt 文本    -> 2 事件（stt, tts 播报）
      interrupt   -> 1 事件（interrupt）
      stop        -> 1 事件（ended）
    """
    token = _auth(client)
    with client.websocket_connect(f"/api/v1/voice/ws?token={token}") as ws:
        # 握手即发 start（LISTENING）：自动进入 listen 态，无需显式 start
        ev = ws.receive_json()
        assert ev["state"] == "LISTENING"

        # 语音帧 + 静音帧（41 帧 -> 恰好 1 个 vad_end）
        ws.send_bytes(b"customer says 128 yuan")
        silence = b"\x00" * 800
        for _ in range(40):
            ws.send_bytes(silence)
        ev = ws.receive_json()
        assert ev["type"] == "vad_end"
        assert ev["state"] == "STT"

        # stt 文本 -> 2 事件：stt（LLM 态）+ tts 播报
        ws.send_json({"type": "stt", "text": "我要退款，订单 ORD-20260901，金额 128 元"})
        ev = ws.receive_json()
        assert ev["type"] == "stt"
        assert ev["state"] == "LLM"
        ev2 = ws.receive_json()  # tts 播报
        assert ev2["type"] == "tts"

        # interrupt 插话：清空 TTS 队列
        ws.send_json({"type": "interrupt"})
        ev3 = ws.receive_json()
        assert ev3["type"] == "interrupt"
        assert ev3["state"] == "INTERRUPTED"
        assert ev3["tts_cleared"] >= 1

        # stop -> ended（1 事件）
        ws.send_json({"type": "stop"})
        ev4 = ws.receive_json()
        assert ev4["type"] == "ended"


def test_voice_create_test_case_eval_no_refund(client):
    """语音创建测试工单：EVAL 打标 + 无真实退款（不启动图）。"""
    import uuid

    token = _auth(client)
    order_id = f"ORD-V-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/voice/create-test-case",
        data={"session_id": f"vs-{uuid.uuid4().hex[:8]}",
              "order_id": order_id, "amount": "99.00"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    case_id = payload["case_id"]
    assert payload["eval_run_id"].startswith("voice-eval-")
    assert payload["eval_case_id"].startswith("vc-")
    assert payload["status"] == "RUNNING"
    assert "未触发任何退款" in payload["note"]

    # EVAL 打标 + 无 refund_transactions（测试单不启动图）
    s = SessionTest()
    try:
        case = s.get(RefundCase, case_id)
        assert case is not None
        assert case.eval_run_id == payload["eval_run_id"]
        assert case.eval_case_id == payload["eval_case_id"]
        txs = s.query(RefundTransaction).filter_by(case_id=case_id).count()
        assert txs == 0, "语音测试单不得触发退款"
        # voice_sessions 未建 case 引用（create-test-case 独立落 crime）
        vs = s.query(VoiceSession).filter_by(created_case_id=case_id).first()
        assert vs is None  # 路由直接建 case，不走 voice_session.case_id
    finally:
        s.close()


def test_voice_coaching_start_and_score(client):
    """陪练：start 生成会话 -> score 5 维评分（只读建议，无副作用）。"""
    import uuid

    token = _auth(client)
    r1 = client.post("/api/v1/voice/coaching/start",
                     data={"scene_id": "VOICE_SCENE_REFUND",
                           "difficulty": "hard", "emotion": "escalated"},
                     headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200, r1.text
    start = r1.json()
    assert start["scene_id"] == "VOICE_SCENE_REFUND"
    assert start["scene_name"] == "退赔处理"
    assert start["difficulty"] == "hard" and start["emotion"] == "escalated"
    sid = start["coaching_session_id"]

    turns = _json.dumps([
        {"reply": "您好，请提供订单号与金额，我核实后为您登记退款。", "prompt": "我要退款"},
        {"reply": "请放心，我会尽快处理并通知您结果。", "prompt": "多久能退"},
    ])
    r2 = client.post("/api/v1/voice/coaching/score",
                     data={"coaching_session_id": sid, "turns": turns},
                     headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    score = r2.json()
    assert set(score["score"].keys()) == {"专业", "情绪", "合规", "结案", "效率"}
    assert score["suggestion"]
    # V2.0：难度/情绪随 start 继承 + 每维解释 + 回放标识（= 行 id）
    assert score["difficulty"] == "hard" and score["emotion"] == "escalated"
    assert score["replay_session_id"] == sid
    assert set(score["explain"].keys()) == {"专业", "情绪", "合规", "结案", "效率"}

    # 落库 coaching_sessions（脱敏）+ 无副作用：找评分会话（suggestion 非空）
    s = SessionTest()
    try:
        row = s.get(CoachingSession, sid)
        assert row is not None
        assert row.difficulty == "hard" and row.emotion == "escalated"
        assert row.scene_name == "退赔处理"
        assert set(row.score_json.keys()) == {"专业", "情绪", "合规", "结案", "效率"}
        assert row.suggestion
    finally:
        s.close()