# -*- coding: utf-8 -*-
"""语音单会话闭环（基线 V1.0）。

设计（裁决 D2：基线用 WebSocket + Mock VAD/STT/TTS，不接真 WebRTC/引擎）：
- WS 二进制帧承载 Mock PCM 音频；文本帧承载控制信令（VAD 事件 / interrupt / 会话结束）；
- MockVAD：入帧标记开始，静音 >= VAD_SILENCE_MS(1500ms) 判定一个语音段结束断点；
  - 无真时域能量分析，以帧字节非空模拟“有语音”、空帧/静音帧模拟“无语音”。
- MockSTT：把语音段文本做确定性识别（模拟理解“订单号/金额/退款”）；
- MockTTS：生成事件输出（TTS 文本由业务层给出，Mock 引擎不产出声学帧）。
- interrupt（核心）：TTS 播放期间若检测到新输入（用户插话）-> 立即清空 TTS 队列、
  状态置 INTERRUPTED + tts_cleared+=1，前端收到反馈继续采集。物理打断指标记录。
- 会话完整生命周期：LISTENING -> (输入) STT -> (识别) LLM -> (回复生成) TTS
  -> INTERRUPTED(插话) -> ENDED(结束回收)。会话超时/上限由调用方（路由 WS）控制。

创建测试工单：
- voice 会话识别确认关键信息（订单号 + 金额）后，创建 **测试工单**（EVAL 打标，
  红线：绝不触发真实退款）。由路由调 create_refund 并以 EVAL headers 标记。

红线：本模块不改变任何资金/退款业务状态；不产出声学，只产出事件。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field

from app.core.config import settings

# ---- 会话状态常量（与 voice_sessions.state 对齐）----
VOICE_LISTENING = "LISTENING"
VOICE_STT = "STT"
VOICE_LLM = "LLM"
VOICE_TTS = "TTS"
VOICE_INTERRUPTED = "INTERRUPTED"
VOICE_ENDED = "ENDED"
VOICE_HANDOFF = "HUMAN_HANDOFF" # V2.0 -5：转人工接管

# ---- 控制信令（文本帧 type 字段）----
SIG_AUDIO = "audio"          # 二进制 PCM 帧（Mock 模拟）
SIG_VAD_END = "vad_end"      # 语音段结束（VAD 断点）
SIG_INTERRUPT = "interrupt"  # 用户插话（打断 TTS）
SIG_START = "start"          # 会话开始
SIG_STOP = "stop"            # 会话结束
SIG_CAPTION = "caption"      # V2.0：实时字幕（增量 STT 片段）
SIG_WEAK = "weak"            # V2.0：弱网标记
SIG_HANDOFF = "human_handoff"  # V2.0：转人工
SIG_RECONNECT = "reconnect"  # V2.0：断线重连（会话恢复）

# 会话关闭原因（voice_sessions.closed_reason）
CLOSE_TIMEOUT = "timeout"
CLOSE_RECONNECT = "reconnect"
CLOSE_END = "end"
CLOSE_HANDOFF = "handoff"

# 会话关闭原因合法集合（路由持久化校验）
VALID_CLOSED_REASONS = frozenset({CLOSE_TIMEOUT, CLOSE_RECONNECT, CLOSE_END, CLOSE_HANDOFF})

# ---- V3-5 调度优先级（：业务实时推理 > 离线评测 > 陪练）----
PRI_BUSINESS = "business"      # 实时业务语音（最高优先）
PRI_EVAL = "eval"              # 离线语音评测（次优先）
PRI_COACHING = "coaching"      # 客服陪练（最低优先）
_PRIORITY_RANK = {PRI_BUSINESS: 0, PRI_EVAL: 1, PRI_COACHING: 2}


class VoiceQueueFull(Exception):
    """排队已满：等待队列达 VOICE_QUEUE_CAPACITY -> 系统过载 DROP（不无限排队）。"""


class VoiceQueueTimeout(Exception):
    """排队等待超时 VOICE_QUEUE_TIMEOUT_SECONDS -> 回 429（客户可稍后重试）。"""


class VoiceProviderDegraded(Exception):
    """主 Provider 熔断打开且非业务会话被拒（熔断降级路径）。"""


class VoiceBreaker:
    """Provider 熔断器（V3-5 熔断降级）。

    状态机：closed -> open（连续失败 >= fail_threshold）-> half_open（冷却后放行一次
    probe）-> closed（probe 成功复位）或 re-open（probe 失败）。提供方为 Mock，正常
    不失败；故障注入通过 record_failure/trip 驱动，判定逻辑与生产一致。
    """

    def __init__(self, fail_threshold: int | None = None,
                 cooldown_seconds: float | None = None):
        self.fail_threshold = fail_threshold or int(settings.VOICE_BREAKER_FAIL_THRESHOLD)
        self.cooldown_seconds = (cooldown_seconds
                                 if cooldown_seconds is not None
                                 else settings.VOICE_BREAKER_COOLDOWN_SECONDS)
        self._failures = 0
        self._opened_at: float | None = None
        self._closed_after_probe = False

    # ---- 状态查询（降级判定）----
    def is_open(self) -> bool:
        """当前主 Provider 是否不可用（供路由决定是否降级/拒绝）。"""
        if self._failures < self.fail_threshold:
            return False                      # closed
        if self._opened_at is None:
            self._opened_at = time.time()
        if self._closed_after_probe:
            return True                       # 探测失败，重新 open（冷却重计）
        if time.time() - self._opened_at >= self.cooldown_seconds:
            return False                      # half_open：放行一次 probe（允许请求）
        return True                           # open（冷却窗口内）

    @property
    def failures(self) -> int:
        return self._failures

    def record_failure(self) -> int:
        """记录一次失败；达到阈值则首次置 opened_at（触发熔断）。"""
        self._failures += 1
        if self._failures >= self.fail_threshold and self._opened_at is None:
            self._opened_at = time.time()
        return self._failures

    def record_success(self) -> None:
        """探测/正常成功：复位计数回到 closed。"""
        self._failures = 0
        self._opened_at = None
        self._closed_after_probe = False

    def trip(self) -> None:
        """强制熔断（测试/人力介入用）。"""
        self._failures = max(self._failures, self.fail_threshold)
        self._opened_at = self._opened_at or time.time()
        self._closed_after_probe = False

    def reset(self) -> None:
        self.record_success()


# 模块级 Provider 熔断器单例（路由/测试共用；测试可重绑定或 monkeypatch）
voice_breaker: "VoiceBreaker" = VoiceBreaker()


@dataclass
class VoiceEvent:
    """会话流转事件（路由用 JSON 回传前端）。"""
    type: str                    # vad_end / stt / llm / tts / interrupt / ended
    session_id: str
    state: str
    text: str | None = None
    reply: str | None = None
    tts_cleared: int = 0
    timestamp: float = field(default_factory=time.time)


class MockVAD:
    """模拟 VAD：以输入帧非空判“有语音”，静音帧累计 >= 阈值判段结束。

    无真时域能量分析（基线 Mock），用**帧累计**模拟时间维度（每帧视为一个
    固定采样间隔约 50ms 的音频块），测试与运行均确定性：
      - 有语音帧（bytes 非空）-> 重置静音帧计数；
      - 空/纯静音帧 -> 累计静音帧数，达到阈值静音时长冒泡 vad_end。
    阈值换算：VAD_SILENCE_MS / 帧间隔(50ms) 个静音帧（默认 1500/50=30 帧）。
    """

    _FRAME_MS = 50  # 单帧模拟采样间隔（50ms）

    def __init__(self, silence_ms: int = None):
        self._silence_ms = silence_ms or settings.VAD_SILENCE_MS
        self._silence_frames = max(1, self._silence_ms // self._FRAME_MS)
        self._in_speech = False
        self._silence_count = 0
        # V3-5 链路插桩：语音停止（首个静音帧）到判段结束的耗时（毫秒）
        self._silence_started_at: float | None = None
        self.vad_latency_ms: float | None = None

    def feed(self, frame: bytes, now: float | None = None) -> bool:
        """入帧。返回 True 表示当前语音段结束（vad_end 断点）。"""
        if frame and frame != b"\x00" * len(frame):
            # 有语音：重置静音计数
            self._in_speech = True
            self._silence_count = 0
            self._silence_started_at = None
            return False
        # 静音帧
        if not self._in_speech:
            return False
        if self._silence_started_at is None:
            self._silence_started_at = time.time()   # 语音停止时刻
        self._silence_count += 1
        if self._silence_count >= self._silence_frames:
            self.vad_latency_ms = (time.time() - self._silence_started_at) * 1000.0
            self._in_speech = False
            self._silence_count = 0
            self._silence_started_at = None
            return True
        return False


class MockSTT:
    """模拟 STT：确定性识别语音段文本中的关键信息。

    模式：识别“订单号 ORD-xxx”“金额 xxx 元”“我要退款/退钱”等。
    txt 由调用方传入（模拟 ASR 结果）；本类做关键词提取。
    """

    def __init__(self):
        pass

    def transcribe(self, text: str | None = None) -> dict:
        """返回 {text, order_id, amount}。无输入则给占位陈述（模拟兜底）。"""
        import re

        raw = text or ""
        order = None
        m = re.search(r"(?i)(ORD|订单号?)[\-号:：\s]*([A-Z0-9\-]{4,16})", raw)
        if m:
            body = m.group(2).upper()
            order = body if body.startswith("ORD-") else f"ORD-{body}"
        amount = None
        m = re.search(r"(\d{2,6}(?:\.\d{1,2})?)\s*元", raw)
        if m:
            amount = float(m.group(1))
        # Mock 文本若无关键信息，给一个可确认的兜底（模拟理解不了转人工确认）
        return {
            "text": raw or "客户申请退款",
            "order_id": order,
            "amount": amount,
        }


class MockTTS:
    """模拟 TTS：不产出声学帧，仅记录“要播放的文本”供打断测试。"""

    def __init__(self):
        self.queue: list[str] = []

    def speak(self, text: str) -> str:
        self.queue.append(text)
        return text

    def clear(self) -> int:
        """清空 TTS 队列（插话打断调用），返回清掉的条数。"""
        n = len(self.queue)
        self.queue.clear()
        return n


class VoiceSessionService:
    """单会话状态机 + Mock 管道 + interrupt 管理（V2.0 -5 增强）。

    新增能力：trace（全程事件审计，不串 Trace）、字幕（partial STT）、弱网标记、
    断线重连（reconnects 计数）、静音超时、转人工。
    """

    def __init__(self, session_id: str | None = None,
                 provider: str = "primary", priority: str = PRI_BUSINESS):
        self.session_id = session_id or f"vs-{uuid.uuid4().hex[:16]}"
        self.state = VOICE_LISTENING
        self.stt_text: str | None = None
        self.tts_cleared = 0
        self.created_at = time.time()
        self.ended_at: float | None = None
        # V2.0 增强字段（与 voice_sessions 列对齐）
        self.reconnects = 0
        self.weak_signal = False
        self.closed_reason: str | None = None
        self.trace: list[dict] = []          # 全程事件审计（不串 Trace 红线）
        self.handoff_reason: str | None = None
        # V3-5 调度/降级元数据
        self.priority = priority
        self.provider = provider
        self.degraded_provider = (provider == "fallback")   # 主 Provider 熔断降级标记
        # V3-5 链路插桩：各段实测耗时（秒），未测得为 None（诚实口径）
        self.segments: dict[str, float | None] = {
            "vad_end": None, "stt": None, "llm_first": None, "tts_first": None,
        }
        self._t0_start = time.time()
        self._llm_entered_at: float | None = None
        self._first_tts_at: float | None = None
        # Mock 管道（fallback 降级 Provider：无 VAD 音频段，直接文本认取）
        self.vad = None if self.degraded_provider else MockVAD()
        self.stt = MockSTT()
        self.tts = MockTTS()
        self._last_stt: dict = {}
        self._done = False

    # ---- 事件审计（Trace）----
    def _emit(self, type_: str, state: str, **extra) -> VoiceEvent:
        """构造事件 + 记入 trace。timestamp 统一冻结，供恢复/审计对账。"""
        ev = VoiceEvent(type=type_, session_id=self.session_id, state=state, **extra)
        self.trace.append({
            "type": ev.type,
            "state": ev.state,
            "text": ev.text,
            "reply": ev.reply,
            "tts_cleared": ev.tts_cleared,
            "timestamp": ev.timestamp,
        })
        return ev

    # ---- 收包（路由 WS 调用）----
    def on_start(self) -> VoiceEvent:
        self.state = VOICE_LISTENING
        return self._emit(SIG_START, self.state)

    def on_audio(self, frame: bytes) -> VoiceEvent | None:
        """二进制音频帧。返回 vad_end 事件（若 VAD 判段结束）。

        降级 Provider（fallback）无 VAD：忽略音频帧，直接等文本信令。"""
        if self._done or self.vad is None:
            return None
        if self.vad.feed(frame):
            self.state = VOICE_STT
            if self.vad.vad_latency_ms is not None:
                # VAD 断点预算：语音停止 -> 判定（Mock 按真实帧推进计时）
                self.segments["vad_end"] = self.vad.vad_latency_ms / 1000.0
            return self._emit(SIG_VAD_END, self.state)
        return None

    def on_stt(self, text: str) -> VoiceEvent:
        """识别完成，STT -> LLM（Mock：直接给确认文案，不引真实 LLM）。"""
        t0 = time.time()
        self.state = VOICE_STT
        self.stt_text = text
        self._last_stt = self.stt.transcribe(text)
        self.segments["stt"] = time.time() - t0
        self.state = VOICE_LLM
        self._llm_entered_at = time.time()
        reply = self._build_reply(self._last_stt)
        # LLM 首 Token（Mock 同步产出，首 Token = 回复就绪时刻）
        self.segments["llm_first"] = time.time() - t0
        return self._emit("stt", self.state, text=text, reply=reply)

    def on_llm(self) -> VoiceEvent:
        """业务回复产出（Mock 不引 LLM），进入 TTS 播报态。"""
        if self._llm_entered_at is not None:
            self.segments["llm_first"] = time.time() - self._llm_entered_at
        self.state = VOICE_TTS
        return self._emit("llm", self.state,
                          reply=self._build_reply(self._last_stt or {}))

    def on_tts(self) -> VoiceEvent:
        """进入 TTS 播报（Mock 引擎入队）；TTS 首包 = 入队完成时刻。"""
        t0 = time.time()
        self.state = VOICE_TTS
        text = self._build_reply(self._last_stt or {})
        self.tts.speak(text)
        self.segments["tts_first"] = time.time() - t0
        if self._first_tts_at is None:
            self._first_tts_at = time.time()          # 端到端首响应完成时刻（仅首条）
        return self._emit("tts", self.state, reply=text)

    # ---- V3-5 链路预算报告（各段实测 vs 预算，未测得如实 None）----
    def pipeline_report(self) -> dict:
        """链路分段预算报告：VAD≤150ms / STT≤200ms / LLM首Token≤300ms / TTS首包≤200ms。"""
        budget = {
            "vad_end": settings.VOICE_VAD_BUDGET_MS / 1000.0,
            "stt": settings.VOICE_STT_BUDGET_MS / 1000.0,
            "llm_first": settings.VOICE_LLM_FIRST_TOKEN_BUDGET_MS / 1000.0,
            "tts_first": settings.VOICE_TTS_FIRST_FRAME_BUDGET_MS / 1000.0,
        }
        seg = {}
        for key in ("vad_end", "stt", "llm_first", "tts_first"):
            val = self.segments.get(key)
            seg[key] = {
                "seconds": round(val, 4) if val is not None else None,
                "budget_seconds": budget[key],
                "budget_ms": int(budget[key] * 1000),
                "meets": (val is not None and val <= budget[key]),
            }
        return {
            "segments": seg,
            "degraded_provider": self.degraded_provider,
            "end_to_end_first_response_seconds": (
                round(self._first_tts_at - self.created_at, 4)
                if self._first_tts_at is not None else None),
        }

    def on_interrupt(self) -> VoiceEvent:
        """用户插话：清空 TTS 队列 + tts_cleared+=1 + 状态置 INTERRUPTED。"""
        cleared = self.tts.clear()
        self.tts_cleared += cleared
        self.state = VOICE_INTERRUPTED
        return self._emit(SIG_INTERRUPT, self.state,
                          tts_cleared=self.tts_cleared, reply="（已打断播报，请讲）")

    # ---- V2.0 增强信令 ----
    def on_caption(self, text: str) -> VoiceEvent:
        """实时字幕：增量 STT 片段（不改变会话状态）。"""
        return self._emit(SIG_CAPTION, self.state, text=text or "")

    def on_weak(self) -> VoiceEvent:
        """弱网标记：置 weak_signal（Telemetry 记录弱网发生）。"""
        self.weak_signal = True
        return self._emit("weak_signal", self.state)

    def on_handoff(self, reason: str = "") -> VoiceEvent:
        """转人工：状态切 HUMAN_HANDOFF + closed_reason 置 handoff。
        只产生接管事件与提示（真实人工队列由业务侧接入），绝不触发退款。"""
        self.state = VOICE_HANDOFF
        self.closed_reason = CLOSE_HANDOFF
        self.handoff_reason = reason
        self._done = True
        self.ended_at = time.time()
        return self._emit(SIG_HANDOFF, self.state,
                          reply=f"已转人工接管（{reason or '客户要求人工'}），请稍候。")

    def on_timeout(self) -> VoiceEvent:
        """静音空闲超时：收口会话（closed_reason=timeout），由路由定时器触发。"""
        self.state = VOICE_ENDED
        self.closed_reason = CLOSE_TIMEOUT
        self._done = True
        self.ended_at = time.time()
        return self._emit("timeout", self.state,
                          reply="长时间无语音，会话已自动结束。")

    def recover(self) -> int:
        """断线重连：reconnects+=1，返回新计数（会话状态可继续）。"""
        self.reconnects += 1
        return self.reconnects

    def on_reconnect(self) -> VoiceEvent:
        """断线重连信令（WS 内触发）：reconnects+=1 + 记 Trace（不改变会话状态）。"""
        self.reconnects += 1
        return self._emit(SIG_RECONNECT, self.state,
                          reply=f"已重连，reconnects={self.reconnects}")

    def on_stop(self) -> VoiceEvent:
        """会话结束：回收（TTL/上限由路由做；行为上只收口状态）。"""
        self.state = VOICE_ENDED
        self.closed_reason = self.closed_reason or CLOSE_END
        self._done = True
        self.ended_at = time.time()
        return self._emit("ended", self.state)

    # ---- 内部 ----
    def _build_reply(self, info: dict) -> str:
        """业务回复（Mock 确定性文案）。涉及敏感信息一律数字打码。"""
        order = (info.get("order_id") or "订单号")
        amount = info.get("amount")
        if amount is None:
            return "请您提供订单号与退款金额，我帮您登记。"
        return f"您申请对订单 {order} 退款 {self._mask_amount(amount)} 元，我登记为测试工单。"

    @staticmethod
    def _mask_amount(amount: float) -> str:
        """金额打码：超过 100 显示主角，其余全码（防侧录）。"""
        if amount >= 100:
            return f"{int(amount // 100) * 100}+"
        return "**"

    # ---- 持久化回读（路由端落 voice_sessions 行）----
    def to_row(self, *, case_id: str | None = None,
               created_case_id: str | None = None) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "stt_text": self.stt_text,
            "tts_cleared": self.tts_cleared,
            "created_case_id": created_case_id,
            "case_id": case_id,
            "reconnects": self.reconnects,
            "weak_signal": self.weak_signal,
            "closed_reason": self.closed_reason,
        }

    # ---- 会话状态快照（恢复/审计查看）----
    def snapshot(self, *, user_id: str = "") -> dict:
        """只读会话快照：状态 + 关键字段 + 全程 Trace（用于断线恢复 / 审计）。"""
        return {
            "session_id": self.session_id,
            "state": self.state,
            "stt_text": self.stt_text,
            "tts_cleared": self.tts_cleared,
            "reconnects": self.reconnects,
            "weak_signal": self.weak_signal,
            "closed_reason": self.closed_reason,
            "priority": self.priority,
            "provider": self.provider,
            "degraded_provider": self.degraded_provider,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "trace": list(self.trace),
        }


class VoiceCapacityExceeded(Exception):
    """语音并发已达上限（拒绝新建会话）。路由转 429 capacity_full。"""

    def __init__(self, active: int, max_c: int):
        super().__init__(f"voice capacity exceeded: active={active} max={max_c}")
        self.active = active
        self.max_concurrent = max_c


@dataclass
class VoiceQueueWaiter:
    """排队等待者（V3-5：容量超限 -> 排队而非直接 429）。

    持有一个 asyncio.Event，由 drop() 触发 _promote() 在腾出容量时按优先权授让。
    """
    priority: str
    order: int                       # FIFO 序号（同优先权内先到先得）
    queue_entered_at: float = field(default_factory=time.time)
    admitted: bool = False
    event: object = None             # asyncio.Event（admit_async 等待用；同步路径为 None）
    created_svc: object = None       # _promote() 授让时创建的实际会话（回传给 admit_async）


class VoiceSessionManager:
    """语音会话注册表（V3-5 ：30-40 路并发 + 排队而非 429）。

    - 硬上限：`VOICE_MAX_CONCURRENT`（默认 32）。容量满时**进入队列等待**
      （不直接 429）：队列满载（VOICE_QUEUE_CAPACITY）才 DROP（VoiceQueueFull）；
    - 优先权：business > eval > coaching，腾出容量时按优先权授让（业务实时优先）；
    - 每会话独立 VoiceSessionService（含独立 Trace 与管道）——并发不串音/不串 Trace；
    - 记录峰值并发（供 Telemetry PEAK_CONCURRENCY）；
    - 断线重连：get(session_id) 命中则 recover，无则新建（新的连接继承会话上下文）。

    入口约定：
    - `create()` / `reconnect()`（同步路径，如 HTTP recovery 端点）：容量满抛
      VoiceCapacityExceeded（保持原 429 语义）；
    - `admit(priority)` / `admit_async(...)`（WS 实时路径）：容量满排队，队列满抛
      VoiceQueueFull，排队超时抛 VoiceQueueTimeout。
    """

    def __init__(self, max_concurrent: int | None = None,
                 queue_capacity: int | None = None,
                 queue_timeout_seconds: float | None = None):
        self.max_concurrent = max_concurrent or int(settings.VOICE_MAX_CONCURRENT)
        self.queue_capacity = (queue_capacity
                               if queue_capacity is not None
                               else int(settings.VOICE_QUEUE_CAPACITY))
        self.queue_timeout_seconds = (queue_timeout_seconds
                                      if queue_timeout_seconds is not None
                                      else settings.VOICE_QUEUE_TIMEOUT_SECONDS)
        self._sessions: dict[str, VoiceSessionService] = {}
        self._waiters: list[VoiceQueueWaiter] = []
        self._waiter_seq = 0
        self._peak = 0

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def peak(self) -> int:
        return self._peak

    @property
    def queue_length(self) -> int:
        return len(self._waiters)

    @property
    def queued_sessions(self) -> list[VoiceQueueWaiter]:
        return list(self._waiters)

    # ---- 同步路径（HTTP recovery 等）：容量满即抛，保持 429 语义 ----
    def create(self, priority: str = PRI_BUSINESS) -> VoiceSessionService:
        if self.active_count >= self.max_concurrent:
            raise VoiceCapacityExceeded(self.active_count, self.max_concurrent)
        svc = VoiceSessionService(priority=priority)
        self._sessions[svc.session_id] = svc
        self._peak = max(self._peak, self.active_count)
        return svc

    def get(self, session_id: str | None) -> VoiceSessionService | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def reconnect(self, session_id: str | None) -> VoiceSessionService:
        """断线重连恢复：命中现有会话 -> recover() 复用；否则新建会话。"""
        svc = self.get(session_id)
        if svc is None:
            svc = self.create()
        else:
            svc.recover()
        return svc

    def attach(self, svc: VoiceSessionService) -> None:
        """显式挂入（恢复合成场景）。"""
        self._sessions[svc.session_id] = svc
        self._peak = max(self._peak, self.active_count)

    def drop(self, session_id: str | None) -> None:
        """回收会话；随后将容量让给最高优先队列等待者（业务实时优先）。"""
        if session_id is not None:
            self._sessions.pop(session_id, None)
        self._promote()

    def _promote(self) -> None:
        """腾出容量后，按优先权（business>eval>coaching）随后 order 授让第一个等待者。"""
        if self.active_count >= self.max_concurrent or not self._waiters:
            return
        self._waiters.sort(key=lambda w: (_PRIORITY_RANK.get(w.priority, 9), w.order))
        w = self._waiters[0]
        self._waiters.pop(0)
        try:
            svc = self.create(priority=w.priority)      # 此时必有空闲位
        except VoiceCapacityExceeded:                   # 竞争理论可能，防御
            self._waiters.insert(0, w)                  # 放回队首
            return
        w.admitted = True
        w.created_svc = svc
        if w.event is not None:
            w.event.set()                               # 唤醒等待协程

    # ---- V3-5 排队入场（同步侧返回状态；异步侧 await 事件）----
    def admit(self, priority: str = PRI_BUSINESS):
        """排队准入。返回 ("granted", svc, None) | ("queued", None, waiter) |
        ("overflow", None, None)。队列满载 -> 调起方应抛 VoiceQueueFull（DROP）。"""
        if self.active_count < self.max_concurrent:
            return ("granted", self.create(priority=priority), None)
        if len(self._waiters) >= self.queue_capacity:
            return ("overflow", None, None)
        self._waiter_seq += 1
        waiter = VoiceQueueWaiter(priority=priority, order=self._waiter_seq)
        self._waiters.append(waiter)
        return ("queued", None, waiter)

    def cancel_waiter(self, waiter: VoiceQueueWaiter) -> bool:
        """排队等待超时/客户端放弃：从等待队列移除（未授让才可取消）。"""
        if waiter.admitted:
            return False
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return False
        return True

    def sessions(self) -> list[VoiceSessionService]:
        return list(self._sessions.values())


async def admit_async(manager: "VoiceSessionManager",
                      priority: str = PRI_BUSINESS,
                      queue_timeout_seconds: float | None = None) -> VoiceSessionService:
    """异步排队准入（WS 实时路径）：容量满 -> 入队等待，超时抛 VoiceQueueTimeout。

    - granted：直接返回会话；
    - overflow：抛 VoiceQueueFull（排队已满 -> 系统性 DROP，回 capacity_full）；
    - queued：轮询 waiter.admitted（异步 sleep 0.02s 粒度），drop() 腾位由 _promote()
      授让（waiter.admitted=True + created_svc）即返回；超时取消等待抛 VoiceQueueTimeout。

    Mock 语义下轮询 = 定时粒度；GIL 下属性读对跨线程授让可见，测试确定性无事件循环漂移。
    """
    import asyncio

    timeout = (queue_timeout_seconds if queue_timeout_seconds is not None
               else manager.queue_timeout_seconds)
    status, svc, waiter = manager.admit(priority)
    if status == "granted":
        return svc
    if status == "overflow":
        raise VoiceQueueFull(
            f"voice queue full: active={manager.active_count} "
            f"waiter={manager.queue_length} capacity={manager.queue_capacity}")
    deadline = time.time() + max(1e-3, timeout)
    while time.time() < deadline:
        if waiter.admitted and waiter.created_svc is not None:
            return waiter.created_svc
        await asyncio.sleep(0.02)
    manager.cancel_waiter(waiter)
    raise VoiceQueueTimeout(
        f"voice queue wait timeout after {timeout:.2f}s "
        f"(active={manager.active_count})")


# 进程级会话管理器单例（V2.0：routes_voice / routes_voice_product / 测试共享）。
# 路由侧统一经 `voice_service.voice_manager` 属性按需引用（非 import 绑定），
# 以便测试通过重绑定该模块属性替换容量参数、避免跨测试状态泄漏。
voice_manager: "VoiceSessionManager" = VoiceSessionManager()


def _session_key(session_id: str) -> str:
    """Redis 会话键（防僵尸连接由 TTL 兜底）。"""
    return f"voice:session:{session_id}"


def session_fingerprint(frame: bytes) -> str:
    """音频帧指纹（Mock 阶段用字节 SHA-256，供审计标识帧来源）。"""
    return hashlib.sha256(frame).hexdigest()[:16]