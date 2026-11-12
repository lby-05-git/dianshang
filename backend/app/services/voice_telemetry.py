# -*- coding: utf-8 -*-
"""语音资源 / 容量基线遥测（V2.0 -5 / V3-5 链路预算）。

采集指标到 `voice_metrics`（与模型 docstring 枚举一致）：
- LATENCY_P99      会话时延（首条 TTS 回复时间 - 会话建立时间）
- ERROR            会话异常关闭（超时/异常/失败）
- PEAK_CONCURRENCY 并发会话峰值
- DROP             容量/队列超限拒绝（VoiceCapacityExceeded / 排队超时）

V3-5 链路分段时延（单会话每段一条，值为秒；预算毫秒见 settings.VOICE_*_BUDGET_MS）：
- VAD_END      语音断点（用户语音停止 -> VAD 判定）预算 ≤150ms
- STT          语音识别时长 预算 ≤200ms
- LLM_FIRST    业务推理首 Token 预算 ≤300ms
- TTS_FIRST    TTS 首包产出 预算 ≤200ms

聚合 `compute_baseline` 出容量基线：P99 / 错误率 / 峰值并发 / Drop 数 / 当前占用率，
另附每段预算 P95 是否达标（未测得如实 None，绝不假报达标）。
供主管页 GET /api/v1/voice/metrics 展示（30-40 路容量基线 + 链路预算看板）。

红线：本模块只写观测数据；绝不触碰退款/评测决策/会话业务内容。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models import VoiceMetric, new_uuid

METRIC_LATENCY_P99 = "LATENCY_P99"
METRIC_ERROR = "ERROR"
METRIC_PEAK_CONCURRENCY = "PEAK_CONCURRENCY"
METRIC_DROP = "DROP"
# ---- V3-5 链路分段 ----
METRIC_VAD_END = "VAD_END"
METRIC_STT = "STT"
METRIC_LLM_FIRST = "LLM_FIRST"
METRIC_TTS_FIRST = "TTS_FIRST"

_KINDS = frozenset({METRIC_LATENCY_P99, METRIC_ERROR,
                    METRIC_PEAK_CONCURRENCY, METRIC_DROP,
                    METRIC_VAD_END, METRIC_STT,
                    METRIC_LLM_FIRST, METRIC_TTS_FIRST})

# 分段预算（秒），与 config 毫秒常量对齐
_SEGMENT_BUDGET_SECONDS = {
    METRIC_VAD_END: settings.VOICE_VAD_BUDGET_MS / 1000.0,
    METRIC_STT: settings.VOICE_STT_BUDGET_MS / 1000.0,
    METRIC_LLM_FIRST: settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS / 1000.0,
    METRIC_TTS_FIRST: settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS / 1000.0,
}


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_metric(
    db: Session, *,
    session_id: str, metric_kind: str, value: float,
    window_start: datetime | None = None, window_end: datetime | None = None,
) -> VoiceMetric:
    """落一条语音指标（只写观测，不改任何业务状态）。"""
    if metric_kind not in _KINDS:
        raise ValueError(f"非法语音指标类型: {metric_kind}")
    now = _now_naive()
    row = VoiceMetric(
        id=new_uuid(),
        session_id=session_id,
        metric_kind=metric_kind,
        value=value,
        window_start=window_start or now,
        window_end=window_end or now,
    )
    db.add(row)
    db.commit()
    return row


_SEGMENT_METRIC_MAP = {
    "vad_end": METRIC_VAD_END,
    "stt": METRIC_STT,
    "llm_first": METRIC_LLM_FIRST,
    "tts_first": METRIC_TTS_FIRST,
}


def record_pipeline_metrics(db: Session, svc) -> list[VoiceMetric]:
    """把一次语音会话的链路分段实测（秒）落为 VAD_END/STT/LLM_FIRST/TTS_FIRST 遥测。

    未测得的分段（如降级 Provider 无 VAD）如实跳过（不编造数值）。只写观测数据。"""
    written: list[VoiceMetric] = []
    for seg, kind in _SEGMENT_METRIC_MAP.items():
        val = (svc.segments or {}).get(seg)
        if val is None:
            continue
        row = record_metric(db, session_id=svc.session_id,
                            metric_kind=kind, value=round(val, 6))
        written.append(row)
    return written


def _window_since(window_minutes: int) -> datetime:
    return _now_naive() - timedelta(minutes=max(1, window_minutes))


def compute_baseline(
    db: Session, *,
    window_minutes: int = 15,
    session_manager=None,
) -> dict:
    """容量基线聚合（只读）：P99 时延 / 错误率 / 峰值并发 / Drop / 当前占用率。

    `session_manager`（VoiceSessionManager）可选注入：给出当前 active 与容量上限，
    用于计算当前占用率；不注入则视观测窗口数据。
    """
    since = _window_since(window_minutes)
    rows = (db.query(VoiceMetric)
            .filter(VoiceMetric.created_at >= since).all())

    latencies = [r.value for r in rows if r.metric_kind == METRIC_LATENCY_P99]
    errors = sum(1 for r in rows if r.metric_kind == METRIC_ERROR)
    drops = sum(1 for r in rows if r.metric_kind == METRIC_DROP)
    peaks = [r.value for r in rows if r.metric_kind == METRIC_PEAK_CONCURRENCY]
    sessions = len(latencies)         # 每个已完成会话落一条 LATENCY_P99

    p99 = _p99(latencies) if latencies else None
    error_rate = (errors / sessions) if sessions else None

    peak_concurrency = max(peaks) if peaks else None

    current_active = None
    max_concurrent = None
    capacity_percent = None
    if session_manager is not None:
        current_active = session_manager.active_count
        max_concurrent = max(session_manager.max_concurrent, 1)
        capacity_percent = round(current_active / max_concurrent, 4)

    # V3-5 链路分段预算：每类样本的 P95 vs 预算（未测得如实 None，不假报达标）
    pipeline = {}
    for kind, budget_sec in _SEGMENT_BUDGET_SECONDS.items():
        vals = [r.value for r in rows if r.metric_kind == kind]
        p95 = _p95(vals) if vals else None
        pipeline[kind] = {
            "samples": len(vals),
            "p95_seconds": round(p95, 4) if p95 is not None else None,
            "budget_ms": int(budget_sec * 1000),
            "meets_budget": (p95 is not None and p95 <= budget_sec),
        }

    return {
        "window_minutes": window_minutes,
        "session_count": sessions,
        "latency_p99_seconds": round(p99, 3) if p99 is not None else None,
        "error_count": errors,
        "error_rate": round(error_rate, 4) if error_rate is not None else None,
        "drop_count": drops,
        "peak_concurrency": peak_concurrency,
        "max_concurrent": max_concurrent,
        "current_active": current_active,
        "capacity_percent": capacity_percent,
        "pipeline_budget": pipeline,
    }


def _p99(values: list[float]) -> float:
    """P99 分位（含边界最少 1 个样本）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(0.99 * len(ordered))))
    return ordered[idx]


def _p95(values: list[float]) -> float:
    """P95 分位（链路分段预算判定用）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(0.95 * len(ordered))))
    return ordered[idx]