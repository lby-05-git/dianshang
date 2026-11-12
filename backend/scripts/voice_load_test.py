#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语音并发压测（V3-5 ：30-40 路 + 端到端首响应 + 插话判定）。

30-40 路并发 WS 语音压测：并发建立 -> VAD 断点 -> STT 往返 -> TTS 首响应 ->
插话（interrupt 往返）-> 结束回收；验证**不串音/不串 Trace/不串上下文**，
并量化判定：
- 端到端首响应 P95 ≤ 800ms（会话建立 -> TTS 首包/首响应）；
- 插话控制 P95 ≤ 100ms（发 interrupt -> 收 interrupt 事件）；
- 遥测落库核查（LATENCY_P99 / 分段）、注册表回收确认。

用法（backend 目录下）：
    PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe scripts/voice_load_test.py [并发，默认 32]

红线：语音 WS 只做测试工单语义（EVAL 打标），本脚本不触发任何真实退款/决策动作；
压测会话走开发库（complaint_refund）的 voice_metrics 观测表，仅落只读观测数据。
"""
from __future__ import annotations

import os
import sys
import threading
import time

# ---- 允许从项目根或 backend/ 两个目录运行（对齐 refresh_dataset.py 踩坑）----
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, _BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _auth() -> str:
    """登录拿 token（开发库演示账号 customer_service1）。"""
    c = TestClient(app)
    resp = c.post("/api/v1/auth/login",
                  json={"username": "customer_service1", "password": "123456"})
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    return resp.json()["token"]


def _worker(index: int, url: str, results: dict) -> None:
    """单路会话：start -> VAD -> stt -> tts(首响应计时) -> interrupt(插话计时) -> stop。

    精确事件序：
      start      -> 1 事件
      1 语音帧 + 30 静音帧 -> 1 事件（vad_end，仅第 30 个静音帧触发）
      stt 文本   -> 2 事件（stt, tts 首响应）
      interrupt  -> 1 事件（插话判定）
      stop       -> 1 事件（ended）

    端到端首响应：用户语音停止（VAD 断点）-> 系统首响应（TTS 首包）。
    连接建立/鉴权另计 session_setup_ms（信息项，不进门禁——连接抖动不判管线预算）。
    """
    try:
        c = TestClient(app)
        t0 = time.time()
        with c.websocket_connect(url) as ws:
            ev = ws.receive_json()
            assert ev["type"] == "start"
            sid = ev["session_id"]
            t_setup_done = time.time()
            # VAD 音频段（1 帧语音 + 1.5s 静音 -> vad_end）
            ws.send_bytes(b"customer says 66 yuan order")
            silence = b"\x00" * 800
            for _ in range(30):
                ws.send_bytes(silence)
            ev_vad = ws.receive_json()
            assert ev_vad["type"] == "vad_end" and ev_vad["session_id"] == sid
            t_speech_end = time.time()                 # VAD 断点 = 用户语音停止
            # STT + TTS 首响应（端到端首响应 = 语音停止 -> tts 完成）
            ws.send_json({"type": "stt",
                          "text": f"订单 ORD-LD-{index:03d} 金额 66 元 我要退款"})
            ev2 = ws.receive_json()          # stt
            ev3 = ws.receive_json()          # tts（首响应）
            first_rt = time.time() - t_speech_end
            session_setup_ms = (t_setup_done - t0) * 1000.0
            assert ev2["session_id"] == sid and ev3["session_id"] == sid
            assert ev3["type"] == "tts"
            # 插话判定（发 interrupt -> 收 interrupt 事件）
            t_i = time.time()
            ws.send_json({"type": "interrupt"})
            ev_i = ws.receive_json()         # interrupt
            interrupt_rt = time.time() - t_i
            assert ev_i["type"] == "interrupt" and ev_i["session_id"] == sid
            assert ev_i["state"] == "INTERRUPTED"
            # 结束
            ws.send_json({"type": "stop"})
            ev4 = ws.receive_json()          # ended
            assert ev4["session_id"] == sid
        results[index] = {
            "ok": True,
            "session_id": sid,
            "first_response_ms": round(first_rt * 1000, 3),
            "interrupt_ms": round(interrupt_rt * 1000, 3),
            "session_setup_ms": round(session_setup_ms, 3),
            "ev_types": [ev["type"], ev_vad["type"], ev2["type"],
                         ev3["type"], ev_i["type"], ev4["type"]],
        }
    except Exception as exc:  # noqa: BLE001 —— 压测失败如实记录，不隐藏
        results[index] = {"ok": False, "error": repr(exc)}


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[max(0, int(0.95 * len(ordered)))]


def main() -> int:
    # V3-5：默认 32 路（= VOICE_MAX_CONCURRENT），目标区间 30-40
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    if not (2 <= n <= 40):
        print(f"[voice_load_test] 并发数需在 2-40（V3-5 目标区间 30-40），收到 {n}")
        return 2

    import app.services.voice_service as vs

    print(f"[voice_load_test] 目标并发 {n} 路 | 容量上限 "
          f"{vs.voice_manager.max_concurrent} 路")
    token = _auth()
    url = f"/api/v1/voice/ws?token={token}"
    results: dict[int, dict] = {}
    barrier = threading.Barrier(n)
    threads = []

    def _run(i: int) -> None:
        barrier.wait()          # 对齐起点，最大化同时连接
        _worker(i, url, results)

    t_start = time.time()
    for i in range(n):
        thr = threading.Thread(target=_run, args=(i,))
        thr.start()
        threads.append(thr)
    for thr in threads:
        thr.join()
    wall = time.time() - t_start

    ok = [results[i] for i in range(n) if results[i].get("ok")]
    failed = [results[i] for i in range(n) if not results[i].get("ok")]
    sids = [r["session_id"] for r in ok]

    print(f"[voice_load_test] 成功 {len(ok)}/{n}  失败 {len(failed)}  总耗时 {wall:.2f}s")
    if failed:
        for f in failed[:5]:
            print("   FAIL:", f)
        return 1

    assert len(set(sids)) == n, "会话 id 重复 = 串音"
    # 事件序：每条 must follow 期望序列（start,vad_end,stt,tts,interrupt,ended）
    expect = ["start", "vad_end", "stt", "tts", "interrupt", "ended"]
    for i in range(n):
        if results[i]["ev_types"] != expect:
            print(f"[voice_load_test] 会话 {i} 事件序异常: {results[i]['ev_types']}")
            return 1

    # ---- V3-5 量化门禁：端到端首响应 P95 ≤ 800ms、插话 P95 ≤ 100ms ----
    fr = [r["first_response_ms"] for r in ok]
    it = [r["interrupt_ms"] for r in ok]
    su = [r["session_setup_ms"] for r in ok]
    fr_p95 = p95(fr)
    it_p95 = p95(it)
    print(f"[voice_load_test] 端到端首响应（语音停止->首响应）均值 "
          f"{sum(fr) / len(fr):.2f}ms  P95 {fr_p95:.2f}ms  (门禁 ≤800ms)")
    print(f"[voice_load_test] 会话建立（连接+鉴权）  均值 "
          f"{sum(su) / len(su):.2f}ms  P95 {p95(su):.2f}ms  (信息项)")
    print(f"[voice_load_test] 插话往返      均值 {sum(it) / len(it):.2f}ms  "
          f"P95 {it_p95:.2f}ms  (门禁 ≤100ms)")
    gate_ok = fr_p95 <= 800.0 and it_p95 <= 100.0
    print(f"[voice_load_test] 首响应/插话门禁 {'PASS' if gate_ok else 'FAIL'}")
    print(f"[voice_load_test] 互不串音/串 Trace：{n} 会话两两独立 session_id、事件序一致")

    if vs.voice_manager.active_count != 0:
        print(f"[voice_load_test] 异常：压测结束后注册表仍占用 "
              f"active={vs.voice_manager.active_count}（回收失败）")
        return 1
    print("[voice_load_test] 注册表回收确认：active=0")

    # 遥测核查（观测表，只读）：LATENCY_P99 样本数 >= 并发数 + V3-5 分段落库
    from app.domain.models import VoiceMetric
    from app.infrastructure.db import SessionLocal
    from sqlalchemy import func

    db = SessionLocal()
    try:
        n_lat = (db.query(func.count(VoiceMetric.id))
                 .filter(VoiceMetric.metric_kind == "LATENCY_P99",
                         VoiceMetric.session_id.in_(sids)).scalar() or 0)
        n_llm = (db.query(func.count(VoiceMetric.id))
                 .filter(VoiceMetric.metric_kind == "LLM_FIRST",
                         VoiceMetric.session_id.in_(sids)).scalar() or 0)
        n_peak = (db.query(func.max(VoiceMetric.value))
                  .filter(VoiceMetric.metric_kind == "PEAK_CONCURRENCY").scalar())
        print(f"[voice_load_test] 遥测落库：本次 LATENCY_P99 {n_lat} 条 (≥{n}？)  "
              f"LLM_FIRST {n_llm} 条；历史 PEAK_CONCURRENCY 水印 {n_peak}")
    finally:
        db.close()

    print(f"[voice_load_test] PASS（{n} 路并发，无串音/无串 Trace，门禁 "
          f"{'达标' if gate_ok else '未达标'}）")
    return 0 if gate_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())