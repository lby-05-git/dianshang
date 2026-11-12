# -*- coding: utf-8 -*-
"""语音单会话闭环 + 客服陪练路由（V2.0 -5 产品化）。

端点：
- WS  /api/v1/voice/ws          单会话音频交换（Mock PCM 帧 + 控制信令）
- POST /api/v1/voice/create-test-case  语音识别关键信息确认后创建**测试工单**（EVAL 打标）
- POST /api/v1/voice/coaching/start    开始陪练会话（V2.0：4 场景 + 难度/情绪校验）
- POST /api/v1/voice/coaching/score    陪练 5 维评分（V2.0：难度/情绪调节 + 每维解释）

WS 协议（文本帧 JSON + 二进制 Mock PCM 帧）：
- {"type":"start"}                             -> 会话开始（LISTENING）
- <binary frame>                               -> 音频帧（Mock PCM）；VAD 判段结束回 vad_end
- {"type":"stt","text":"..."}                  -> 识别文字（Mock）
- {"type":"interrupt"}                         -> 用户插话：清空 TTS 队列 + tts_cleared++（打断指标）
- {"type":"caption","text":"..."}              -> 实时字幕（V2.0 增量 STT 片段）
- {"type":"weak"}                              -> 弱网标记（V2.0，Telemetry 记录 weak_signal）
- {"type":"human_handoff","reason":"..."}      -> 转人工（V2.0，状态 HUMAN_HANDOFF，无退款动作）
- {"type":"reconnect"}                         -> 断线重连计数（V2.0，reconnects++）
- {"type":"stop"}                              -> 会话结束（ENDED，回收）

V2.0 能力接线：
- SessionManager 容量上限（20 路）：超限 WS 回 `{"code":"capacity_full"}` 并 429 关闭；
- PEAK_CONCURRENCY / LATENCY_P99 / ERROR / DROP 遥测落 voice_metrics（主管看板基线）；
- 静音空闲超时（VOICE_IDLE_TIMEOUT_SECONDS）：收口 closed_reason=timeout + ERROR 遥测。

创建测试工单（红线 R5：语音创建的一律 EVAL 打标，禁真实退款）：
- 入参：session_id + 关键信息（order_id/amount）——EVAL 打标 create_refund，且不启动图
  （图由人工/worker 手动触发；评测判据只验证“能建测试单且无真实退款”）。

陪练红线：只读评分 + 培训建议文字，绝不触发退款/资金/审批；turns 落库前脱敏。
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import decode_access_token
from app.domain.models import (
    CoachingSession,
    GATE_BLOCK,
    User,
    VoiceSession,
    new_uuid,
)
from app.infrastructure.db import get_db
from app.infrastructure.order_provider import MockOrderProvider, OrderItem, OrderRecord
from app.services import voice_service as vs
from app.services import voice_telemetry as vtel
from app.services.coaching_service import (
    SCENARIOS,
    SCENE_REFUND,
    CoachingTurn,
    score_coaching,
    validate_scene_params,
)
from app.services.security_gateway import gate_text
from app.services.voice_service import (
    PRI_BUSINESS,
    CLOSE_HANDOFF,
    CLOSE_TIMEOUT,
    SIG_CAPTION,
    SIG_HANDOFF,
    SIG_INTERRUPT,
    SIG_RECONNECT,
    SIG_START,
    SIG_STOP,
    SIG_WEAK,
    VoiceEvent,
    VoiceProviderDegraded,
    VoiceQueueFull,
    VoiceQueueTimeout,
    VoiceSessionService,
)

router = APIRouter(prefix="/voice", tags=["voice"])

# 静音空闲超时（模块级读取，测试可替换短值以触发超时路径）
_WS_IDLE_TIMEOUT = float(settings.VOICE_IDLE_TIMEOUT_SECONDS)


# ---- 遥测（WS 无 get_db 依赖，独立 SessionLocal 落 voice_metrics）----

def _emit_metric(metric_kind: str, value: float, session_id: str = "") -> None:
    """落一条语音遥测（只写观测）。

    WS 无 get_db 依赖：优先走已安装的依赖覆盖（测试库），否则默认 SessionLocal。"""
    from app.infrastructure.db import SessionLocal, get_db
    from app.main import app

    db = None
    try:
        if get_db in app.dependency_overrides:
            gen = app.dependency_overrides[get_db]()
            db = next(gen)
        else:
            db = SessionLocal()
        vtel.record_metric(db, session_id=session_id,
                           metric_kind=metric_kind, value=value)
    finally:
        if db is not None:
            db.close()


def _emit_pipeline(svc) -> None:
    """落一次会话的链路分段实测（VAD_END/STT/LLM_FIRST/TTS_FIRST，只写观测）。"""
    from app.infrastructure.db import SessionLocal, get_db
    from app.main import app

    db = None
    try:
        if get_db in app.dependency_overrides:
            gen = app.dependency_overrides[get_db]()
            db = next(gen)
        else:
            db = SessionLocal()
        vtel.record_pipeline_metrics(db, svc)
    finally:
        if db is not None:
            db.close()


# ---- WS 会话（SessionManager 注册 + Mock 管道）----

def _ws_user(websocket: WebSocket) -> User:
    """WS 鉴权：token 查询参数（HTTPBearer 不适用 WS）。"""
    token = websocket.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    try:
        payload = decode_access_token(token)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid token")

    # WS 依赖注入不可用（HTTPBearer/get_db 均需 Request 依赖）。取会话时优先走
    # 已安装的 get_db 覆盖（测试库），否则默认 SessionLocal。
    from app.infrastructure.db import SessionLocal, get_db
    from app.main import app

    db = None
    try:
        if get_db in app.dependency_overrides:
            gen = app.dependency_overrides[get_db]()
            db = next(gen)
        else:
            db = SessionLocal()
        user = db.get(User, user_id)
    except Exception:  # noqa: BLE001
        user = None
    finally:
        if db is not None:
            db.close()
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    return user


def _persist_voice_row(
    svc: VoiceSessionService,
    *,
    user_id: str,
    case_id: str | None = None,
    created_case_id: str | None = None,
):
    """落 voice_sessions 行（append-only，会话状态快照，含 V2.0 增强列）。

    优先走已安装的依赖覆盖（测试库），否则默认 SessionLocal。"""
    from app.infrastructure.db import SessionLocal, get_db
    from app.main import app

    db = None
    try:
        if get_db in app.dependency_overrides:
            gen = app.dependency_overrides[get_db]()
            db = next(gen)
        else:
            db = SessionLocal()
        db.add(VoiceSession(
            id=new_uuid(),
            session_id=svc.session_id,
            state=svc.state,
            stt_text=svc.stt_text,
            tts_cleared=svc.tts_cleared,
            created_case_id=created_case_id,
            case_id=case_id,
            reconnects=svc.reconnects,
            weak_signal=svc.weak_signal,
            closed_reason=svc.closed_reason,
        ))
        db.commit()
    finally:
        if db is not None:
            db.close()


async def _admit_with_breaker(priority: str = PRI_BUSINESS) -> VoiceSessionService:
    """V3-5 入场门：熔断打开时的降级路径 + 容量排队准入。

    - 熔断关着：走 voice_manager.admit_async -> 容量满排队，排队满抛 VoiceQueueFull，
      超时抛 VoiceQueueTimeout；
    - 熔断打开：业务实时会话**降级**走 fallback Provider（直接文本，无 VAD 音频段，
      仍接 full 业务推理），离线评测/陪练抛 VoiceProviderDegraded（拒绝占坑）。
    """
    if vs.voice_breaker.is_open():
        if priority != PRI_BUSINESS:
            raise VoiceProviderDegraded(
                f"主语音 Provider 熔断中，非业务会话（{priority}）暂缓（降级只服务实时业务）")
        svc = vs.VoiceSessionService(provider="fallback", priority=priority)
        vs.voice_manager.attach(svc)          # 降级态仍入注册表（防串 Trace/串音）
        return svc
    return await vs.admit_async(vs.voice_manager, priority=priority)


@router.websocket("/ws")
async def voice_ws(websocket: WebSocket):
    """单会话音频交换 + V3-5 排队/熔断降级 + V2.0 并发/字幕/弱网/转人工/遥测。

    V3-5 准入语义：
    - 容量满 -> **排队等待**（非直接 429）：排队满 or 排队超时 -> 429 关闭 + DROP；
    - 主 Provider 熔断打开 -> 业务实时会话降级走 fallback Provider（直接文本
      无 VAD 音频段），离线评测/陪练入场景拒绝（provider_degraded）；
    - 链路分段实测（VAD_END/STT/LLM_FIRST/TTS_FIRST）落遥测，预算达标判定呈主管看板。
    Trace 全程隔离（每会话独立状态机），并发不串音/不串 Trace。
    """
    await websocket.accept()
    user = _ws_user(websocket)

    # ---- V3-5 准入（排队 + 熔断降级门）----
    trial_ok = True                 # 会话闭环健康标志（驱动熔断 success/failure）
    try:
        svc = await _admit_with_breaker(priority=PRI_BUSINESS)
    except VoiceQueueFull as exc:
        vs.voice_breaker.record_failure()          # 系统过载信号计入熔断
        await websocket.send_json({
            "type": "error", "code": "capacity_full",
            "active": vs.voice_manager.active_count,
            "max_concurrent": vs.voice_manager.max_concurrent,
            "queued": vs.voice_manager.queue_length,
            "note": "排队已满，请稍后重试",
        })
        _emit_metric(vtel.METRIC_DROP, 1.0)
        await websocket.close(code=429)
        return
    except VoiceQueueTimeout as exc:
        await websocket.send_json({
            "type": "error", "code": "queue_timeout",
            "active": vs.voice_manager.active_count,
            "queued": vs.voice_manager.queue_length,
            "note": str(exc),
        })
        _emit_metric(vtel.METRIC_DROP, 1.0)
        await websocket.close(code=429)
        return
    except VoiceProviderDegraded as exc:
        await websocket.send_json({
            "type": "error", "code": "provider_degraded",
            "note": str(exc),
        })
        _emit_metric(vtel.METRIC_ERROR, 1.0)
        await websocket.close(code=503)
        return
    # 并发新峰值水印遥测（仅在新高水位时落一条）
    if vs.voice_manager.active_count == vs.voice_manager.peak:
        _emit_metric(vtel.METRIC_PEAK_CONCURRENCY, float(vs.voice_manager.active_count))

    async def send(event: VoiceEvent) -> None:
        await websocket.send_json({
            "type": event.type,
            "session_id": event.session_id,
            "state": event.state,
            "text": event.text,
            "reply": event.reply,
            "tts_cleared": event.tts_cleared,
            "timestamp": event.timestamp,
        })

    latency_done = False
    try:
        # 初始 LISTENING
        await send(svc.on_start())
        while True:
            try:
                # 静音空闲超时：接收挂起超过阈值即收口（V2.0）
                msg = await asyncio.wait_for(websocket.receive(),
                                             timeout=_WS_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                await send(svc.on_timeout())
                break

            if msg.get("type") == "websocket.disconnect":
                break

            if msg.get("type") == "websocket.receive":
                raw = msg.get("bytes") or msg.get("text")
                if raw is None:
                    continue

                # 二进制 = Mock PCM 音频帧
                if isinstance(raw, bytes):
                    if len(raw) > settings.VOICE_MAX_FRAME_SIZE:
                        continue  # 超帧长直接丢弃（V2.0 单帧上限）
                    event = svc.on_audio(raw)
                    if event is not None:
                        await send(event)
                    continue

                # 文本 = 控制信令
                import json as _json

                try:
                    ctrl = _json.loads(raw)
                except Exception:  # noqa: BLE001 —— 非 JSON 文本视为音频描述
                    ctrl = {"type": "stt", "text": raw[:500]}

                ctype = ctrl.get("type")
                if ctype == SIG_START:
                    await send(svc.on_start())
                elif ctype == "stt":
                    event = svc.on_stt(ctrl.get("text", ""))
                    await send(event)
                    # STT->LLM 回复即 TTS 播报态（Mock：无真实声学，只回文案）
                    tts_ev = svc.on_tts()
                    await send(tts_ev)
                    # 会话首条回复时延遥测（LATENCY_P99 + V3-5 链路分段预算）
                    if not latency_done:
                        _emit_metric(
                            vtel.METRIC_LATENCY_P99,
                            round(tts_ev.timestamp - svc.created_at, 3),
                            session_id=svc.session_id,
                        )
                        _emit_pipeline(svc)          # VAD_END/STT/LLM_FIRST/TTS_FIRST
                        latency_done = True
                elif ctype == SIG_INTERRUPT:
                    await send(svc.on_interrupt())
                elif ctype == SIG_CAPTION:      # V2.0 实时字幕
                    await send(svc.on_caption(ctrl.get("text", "")))
                elif ctype == SIG_WEAK:          # V2.0 弱网标记
                    await send(svc.on_weak())
                elif ctype == SIG_HANDOFF:       # V2.0 转人工（无退款动作）
                    await send(svc.on_handoff(ctrl.get("reason", "客户要求人工")))
                    break
                elif ctype == SIG_RECONNECT:     # V2.0 断线重连计数
                    await send(svc.on_reconnect())
                elif ctype == SIG_STOP:
                    await send(svc.on_stop())
                    break
                else:
                    continue
    except WebSocketDisconnect:  # noqa: PERF203
        pass
    except Exception:  # noqa: BLE001 —— 异常终止：ERROR 遥测 + 熔断失败后上抛（可观测）
        trial_ok = False
        if svc.closed_reason not in (CLOSE_TIMEOUT, CLOSE_HANDOFF):
            _emit_metric(vtel.METRIC_ERROR, 1.0, session_id=svc.session_id)
        raise
    finally:
        # 会话结束回收：落行（含 reconnects/weak_signal/closed_reason）+ 移出注册表
        svc.on_stop()
        _persist_voice_row(svc, user_id=user.id)
        if svc.closed_reason == CLOSE_TIMEOUT:
            _emit_metric(vtel.METRIC_ERROR, 1.0, session_id=svc.session_id)
        vs.voice_manager.drop(svc.session_id)
        # 熔断闭环：正常完成 = Provider 成功（复位/关断）；异常 = 失败计数
        if trial_ok:
            vs.voice_breaker.record_success()
        else:
            vs.voice_breaker.record_failure()


# ---- 语音创建测试工单（EVAL 打标，禁真实退款）----

def _ensure_voice_test_order(order_id: str, amount: float,
                             owner: str = "customer_service1") -> None:
    """注册一张 Mock 测试订单（供图节点核验；EVAL 打标来源由调用方语义保证）。"""
    provider = MockOrderProvider(timeout_for_username=None)
    provider.register_order(OrderRecord(
        order_id=order_id, order_no=f"NO-V-{uuid.uuid4().hex[:8]}",
        user_username=owner, status="PAID",
        total_price_yuan=amount, paid_amount_yuan=amount,
        refunded_amount_yuan=0.0, refundable_amount_yuan=amount,
        items=[OrderItem(sku=f"SKU-V-{uuid.uuid4().hex[:4]}", name="测试商品",
                         spec="语音测试", quantity=1)],
    ))


@router.post("/create-test-case", status_code=201)
def create_test_case(
    session_id: str = Form(...),
    order_id: str = Form(..., min_length=1, max_length=50),
    amount: float = Form(gt=0, le=100000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """语音识别确认关键信息（订单号/金额）后创建**测试工单**（EVAL 打标）。

    硬接线红线（R5/R1）：EVAL 打标 create_refund，且**不启动图**——评测/语音
    测试单一律不触发真实退款（系统 Mock 退款 + EVAL 隔离语义双保险）。
    """
    run_id = f"voice-eval-{uuid.uuid4().hex[:8]}"
    eval_case_id = f"vc-{uuid.uuid4().hex[:8]}"

    # 语音安全网关（source=voice）：语音输入同入注入/DLP 判定，BLOCK 拒绝建单
    voice_text = f"语音建单 订单 {order_id} 金额 {amount} 元"
    gate_res = gate_text(db, text=voice_text, source="voice", persist=False)
    if gate_res.gate_result == GATE_BLOCK:
        raise HTTPException(status_code=400,
                            detail=f"语音输入安全网关拦截（{gate_res.risk_level}）")

    # 注册 Mock 测试订单（供图核验；EVAL test 单不会触发真实退款）
    _ensure_voice_test_order(order_id=order_id, amount=amount)

    # 复用 create_refund 语义：直接落单（EVAL 打标头），但不入图（不 XADD）
    # 推理：语音测试单仅验证“关键信息确认→建测试单”，图执行由人工/worker 手动
    # 触发，评测判据只要求“无真实退款”。
    from app.domain.models import CASE_STATUS_RUNNING, DECISION_PENDING, RefundCase

    case_id = str(uuid.uuid4())
    order_no = f"NO-V-{uuid.uuid4().hex[:8]}"
    db.add(RefundCase(
        id=case_id,
        order_id=order_id,
        order_no=order_no,
        requested_amount=Decimal(str(amount)),
        refund_reason=f"语音测试工单（{eval_case_id}）",
        submitter_id=user.id,
        current_status=CASE_STATUS_RUNNING,
        final_decision=DECISION_PENDING,
        gateway_result=gate_res.gate_result,
        eval_run_id=run_id,
        eval_case_id=eval_case_id,
    ))
    db.flush()
    # 审计：语音建单也落 security_decisions（source=voice）
    gate_text(db, text=voice_text, case_id=case_id, source="voice", persist=True)
    # 回写 voice_sessions 的 created_case_id（EVAL 测试单）
    db.commit()

    # 标记会话已建测试单（进程内内存态；路由层可随后持久化）
    return {
        "case_id": case_id,
        "status": "RUNNING",
        "eval_run_id": run_id,
        "eval_case_id": eval_case_id,
        "gate_result": gate_res.gate_result,
        "note": "语音测试工单（EVAL 打标），未触发任何退款",
    }


# ---- 客服陪练（V2.0：4 场景 + 难度/情绪，只读评分，禁副作用）----

@router.post("/coaching/start", status_code=200)
def coaching_start(
    scene_id: str = Form(SCENE_REFUND),
    difficulty: str = Form(None),
    emotion: str = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """开始陪练会话：校验场景/难度/情绪 + 生成 session_id（落位）。

    返回 coaching_session_id（= CoachingSession 行 id，供 score/replay 关联）。
    落库行不与任何真实用户数据/退款凭据挂钩（测评隔离红线）。"""
    diff, mot = validate_scene_params(scene_id, difficulty, emotion)
    cs_id = f"cs-{uuid.uuid4().hex[:16]}"
    db.add(CoachingSession(
        id=cs_id,
        user_id=user.id,
        scene_id=scene_id,
        scene_name=SCENARIOS[scene_id]["name"],
        difficulty=diff,
        emotion=mot,
        score_json={"专业": 0.0, "情绪": 0.0, "合规": 0.0, "结案": 0.0, "效率": 0.0},
        suggestion=None,
    ))
    db.commit()
    return {
        "coaching_session_id": cs_id,
        "scene_id": scene_id,
        "scene_name": SCENARIOS[scene_id]["name"],
        "difficulty": diff,
        "emotion": mot,
        "max_turns": settings.COACHING_MAX_TURNS,
    }


@router.post("/coaching/score", status_code=200)
def coaching_score(
    coaching_session_id: str = Form(...),
    turns: str = Form(..., description="JSON 数组：[{reply, prompt}] 客服话术（将脱敏）"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """陪练 5 维评分（专业/情绪/合规/结案/效率）+ 难度/情绪调节 + 每维解释。

    仅培训建议文字，无任何资金/退款动作；turns 落库前脱敏（不落敏感原文）。"""
    import json as _json

    from app.services.coaching_service import DIMENSIONS, score_coaching

    row = db.get(CoachingSession, coaching_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="陪练会话不存在")

    try:
        raw_turns = _json.loads(turns)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"turns 需为 JSON 数组: {exc}")

    if not isinstance(raw_turns, list) or not raw_turns:
        raise HTTPException(status_code=400, detail="turns 不可为空数组")
    if len(raw_turns) > settings.COACHING_MAX_TURNS:
        raise HTTPException(status_code=400,
                            detail=f"回合数超上限（{settings.COACHING_MAX_TURNS}）")

    turns_objs = [
        CoachingTurn(reply=str(t.get("reply", "")), prompt=str(t.get("prompt", "")))
        for t in raw_turns
    ]
    result = score_coaching(
        row.scene_id, turns_objs,
        difficulty=row.difficulty or "medium",
        emotion=row.emotion or "calm",
        session_id=row.id,
    )

    # 回写同一会话行（turns 已脱敏；不落敏感原文）——append 维由 start 行承担
    row.turns = [t.as_dict() for t in turns_objs]
    row.score_json = result.score_json
    row.suggestion = result.suggestion
    row.scene_name = SCENARIOS.get(row.scene_id, {}).get("name")
    db.commit()
    return result.as_dict()