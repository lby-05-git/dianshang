# -*- coding: utf-8 -*-
"""工单接口（docs/06 ）：创建(202) / 列表 / 详情 / 审批 / 拒绝。

创建流程（受理层只做快事）：
  鉴权 -> 幂等占位(SETNX) -> 校验 -> 保存凭证文件 -> 落库 RUNNING -> XADD stream:refund_apply -> 202
审批流程：
  主管权限 -> 幂等占位 -> 乐观锁(WHERE current_status='SUSPENDED') -> 状态机校验
  -> 写 human_interventions -> 落库 COMPLETED+终态 -> XADD stream:human_decision
（Redis 分布式锁与结果缓存在 Phase 7 强化；退款执行由 Worker 消费决策流完成）
"""
import hashlib
import json
import mimetypes
import re
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, get_current_user, require_min_role, require_supervisor
from app.api.schemas import (
    AgentTrace,
    CaseDetail,
    CaseListItem,
    CaseListResponse,
    CheckpointViewResponse,
    OrderLookupItem,
    OrderVerifyResponse,
    ReasonConfirmRequest,
    RefundCreateResponse,
    RefundEventItem,
    RefundStatusResponse,
    RefundTx,
)
from app.core.config import settings
from app.domain.models import (
    CHANNEL_DEFAULT,
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    AgentExecutionLog,
    Evidence,
    HumanIntervention,
    RefundCase,
    RefundTransaction,
    User,
)
from app.domain.state_machine import transition_case
from app.infrastructure.db import get_db
from app.services.security_gateway import gate_text
from app.services.decision_scoring import build_decision_scores
from app.services.multimodal_evidence import normalize_stored_evidence_item, normalize_stored_evidence_set
from app.infrastructure.redis_client import STREAM_REFUND_APPLY, STREAM_HUMAN_DECISION, redis_client, xadd

router = APIRouter(prefix="/refunds", tags=["refunds"])

MAX_AMOUNT = Decimal("100000.00")


def _idempotency_claim(key: str, case_id: str | None = None) -> str | None:
    """SETNX 占位；返回 None 表示首次请求（已占位），否则返回已存在的 case_id。"""
    if case_id is None:
        case_id = str(uuid.uuid4())
    ok = redis_client.set(
        f"idem:{key}", case_id, nx=True, ex=settings.IDEMPOTENCY_TTL_SECONDS
    )
    if ok:
        return None
    return redis_client.get(f"idem:{key}")


def _idempotency_cache_result(key: str, payload: dict) -> None:
    """结果缓存（docs/07 手册 ）：首次请求完成后缓存结果，重试拿到"第一次的结果"。"""
    redis_client.set(f"idem:{key}:result", json.dumps(payload, ensure_ascii=False),
                     ex=settings.IDEMPOTENCY_TTL_SECONDS)


def _idempotency_get_cached(key: str) -> dict | None:
    raw = redis_client.get(f"idem:{key}:result")
    return json.loads(raw) if raw else None


def _security_gate_before_create(db: Session, text: str) -> "object":
    """create_refund 入口输入安全网关纯判定（基线 V1.0）。

    只判定不落库（case 尚未创建，security_decisions.case_id FK 无法引用）。
    返回 SecurityDecisionResult；BLOCK 由调用方建占位 case 后再落库审计。
    """
    from app.services.security_gateway import diagnose

    return diagnose(text or "")


def _record_intent_on_create(db: Session, case_id: str, text: str) -> None:
    """create_refund 入口落两级意图到 refund_cases.intent。"""
    from app.services.intent_router import record_intent_from_rule

    res = record_intent_from_rule(db, case_id=case_id, text=text or "")
    case = db.get(RefundCase, case_id)
    if case is not None:
        case.intent = res.final_intent
        db.commit()


_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _approve_lock(case_id: str, token: str) -> bool:
    """审批分布式排他锁（docs/07 手册 ）：SET NX EX，防双主管并发审批。"""
    return redis_client.set(
        f"lock:approve:{case_id}", token, nx=True, ex=settings.APPROVE_LOCK_TTL_SECONDS
    )


def _approve_unlock(case_id: str, token: str) -> None:
    redis_client.eval(_LOCK_RELEASE_LUA, 1, f"lock:approve:{case_id}", token)


def _safe_original_name(file: UploadFile, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._\u3400-\u9fff-]", "_", Path(file.filename or "附件").stem)
    return f"{stem[:80] or '附件'}{ext}"


def _save_evidence_file(file: UploadFile, case_id: str) -> tuple[str, str]:
    """保存凭证到 uploads/，返回 (相对url, md5)。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in settings.ALLOWED_IMAGE_EXT:
        raise HTTPException(status_code=400, detail="仅支持 jpg/jpeg/png/webp 图片")
    data = file.file.read()
    if len(data) > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 5MB 限制")
    md5 = hashlib.md5(data).hexdigest()
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # UUID + 原始文件名既避免多附件互相覆盖，也便于主管端显示用户文件名。
    fname = f"{case_id}__{uuid.uuid4().hex[:8]}__{_safe_original_name(file, ext)}"
    (upload_dir / fname).write_bytes(data)
    return f"uploads/{fname}", md5


def _evidence_original_name(evidence: Evidence) -> str:
    stored = Path(evidence.file_url).name
    return stored.split("__", 2)[-1] if "__" in stored else stored


def _resolve_evidence_path(evidence: Evidence) -> Path:
    upload_root = Path(settings.UPLOAD_DIR).resolve()
    evidence_path = (upload_root / Path(evidence.file_url).name).resolve()
    if evidence_path.parent != upload_root or not evidence_path.is_file():
        raise HTTPException(status_code=404, detail="订单凭证文件不存在")
    return evidence_path


@router.post("", status_code=202, response_model=RefundCreateResponse)
def create_refund(
    order_id: str = Form(min_length=1, max_length=50),
    order_no: str | None = Form(default=None, max_length=64),
    channel: str | None = Form(default=None, max_length=20),
    reason_codes: str | None = Form(default=None, max_length=500),
    requested_amount: Decimal = Form(gt=0, le=MAX_AMOUNT),
    refund_reason: str | None = Form(default=None, max_length=500),
    evidence: UploadFile | None = File(default=None),
    evidences: list[UploadFile] = File(default=[]),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    x_eval_run_id: str | None = Header(default=None, alias="X-Eval-Run-Id"),
    x_eval_case_id: str | None = Header(default=None, alias="X-Eval-Case-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not x_idempotency_key:
        raise HTTPException(status_code=400, detail="缺少 X-Idempotency-Key")
    upload_files = ([evidence] if evidence is not None else []) + list(evidences or [])
    if len(upload_files) > 6:
        raise HTTPException(status_code=400, detail="每次最多上传 6 张附件")
    case_id = str(uuid.uuid4())
    existing = _idempotency_claim(x_idempotency_key, case_id)
    if existing is not None:
        cached = _idempotency_get_cached(x_idempotency_key)
        if cached:
            return RefundCreateResponse(**cached)  # 幂等：返回第一次的结果
        return RefundCreateResponse(case_id=existing, status="RUNNING")  # 处理中

    # 输入安全网关纯判定——不落库，先判是否 BLOCK。
    gate_res = _security_gate_before_create(db, refund_reason or order_id)
    gate_blocked = (gate_res.gate_result == "BLOCK")

    # 3.3.4/3.3.2：建单链路补渠道与单号字段（V2 缺口属性）
    valid_channels = ("APP", "小程序", "客服代提")
    ch = (channel or CHANNEL_DEFAULT)
    if ch not in valid_channels:
        raise HTTPException(status_code=400, detail=f"未知渠道，允许 {valid_channels}")
    rcs = [c.strip() for c in reason_codes.split(",")] if reason_codes else None
    trace_id = str(uuid.uuid4())

    case = RefundCase(
        id=case_id,
        order_id=order_id,
        order_no=order_no or order_id,
        channel=ch,
        reason_codes=rcs,
        requested_amount=requested_amount,
        refund_reason=refund_reason,
        submitter_id=user.id,
        current_status=(CASE_STATUS_SUSPENDED if gate_blocked else CASE_STATUS_RUNNING),
        final_decision=DECISION_PENDING,
        idempotency_key=x_idempotency_key,
        trace_id=trace_id,
        gateway_result=gate_res.gate_result,
        # 评测关联（M8 隔离按 eval_run_id 过滤；非评测案件为 None）
        eval_run_id=x_eval_run_id or None,
        eval_case_id=x_eval_case_id or None,
    )
    db.add(case)
    db.flush()
    for upload in upload_files:
        url, md5 = _save_evidence_file(upload, case_id)
        db.add(Evidence(case_id=case_id, file_url=url, file_md5=md5))
    # 落 security_decisions 审计（case 已落库，FK 满足）
    gate_text(db, text=refund_reason or order_id, case_id=case_id,
              source="intake", persist=True)
    db.commit()

    # BLOCK：拒绝入图（绝不进入退款流程），返回 400 拒绝理由；case 保留审计痕迹
    if gate_blocked:
        _idempotency_cache_result(x_idempotency_key, {"case_id": case_id, "status": "SUSPENDED"})
        raise HTTPException(
            status_code=400,
            detail=f"输入安全网关拦截（{gate_res.risk_level}），案件已记录并挂起复核",
        )

    # 落两级意图到 refund_cases.intent；仅规则层，不引 LLM
    _record_intent_on_create(db, case_id, refund_reason or order_id)

    xadd(STREAM_REFUND_APPLY, {"case_id": case_id, "trace_id": trace_id})
    _idempotency_cache_result(x_idempotency_key, {"case_id": case_id, "status": "RUNNING"})
    return RefundCreateResponse(case_id=case_id)


@router.get("", response_model=CaseListResponse)
def list_cases(
    status: str | None = None,
    page: int = 1,
    size: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if page < 1 or size < 1 or size > 100:
        raise HTTPException(status_code=400, detail="分页参数非法")
    q = db.query(RefundCase).filter(RefundCase.non_eval())
    if status:
        q = q.filter_by(current_status=status)
    total = q.count()
    rows = q.order_by(RefundCase.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return CaseListResponse(
        total=total,
        items=[
            CaseListItem(
                case_id=r.id, order_id=r.order_id, requested_amount=r.requested_amount,
                current_status=r.current_status, final_decision=r.final_decision,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


@router.get("/{case_id}", response_model=CaseDetail)
def get_case(case_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    traces = (
        db.query(AgentExecutionLog)
        .filter_by(case_id=case_id)
        .order_by(AgentExecutionLog.step_order)
        .all()
    )
    txs = (
        db.query(RefundTransaction)
        .filter_by(case_id=case_id)
        .order_by(RefundTransaction.created_at)
        .all()
    )
    # 历史工单可能在旧版 VL 分类器中把结构化电子订单凭证保存成 other。
    # 详情页采用基于原始 OCR 字段的只读派生视图，保留数据库与 Agent Trace 原始审计事实。
    display_ocr_summary = normalize_stored_evidence_set(case.ocr_summary)
    trace_outputs = {
        trace.agent_name: (trace.output_snapshot or {})
        for trace in traces
    }
    fraud_output = trace_outputs.get("fraud", {})
    sentiment_output = trace_outputs.get("sentiment", {})
    return CaseDetail(
        case_id=case.id, order_id=case.order_id, requested_amount=case.requested_amount,
        refund_reason=case.refund_reason, current_status=case.current_status,
        final_decision=case.final_decision,
        refund_status=case.refund_status,
        current_graph_node=case.current_graph_node,
        fraud_score=case.fraud_score, sentiment_level=case.sentiment_level,
        ocr_summary=display_ocr_summary,
        decision_scores=build_decision_scores(
            ocr_summary=display_ocr_summary,
            order_verification=case.order_verification,
            product_verification=case.product_verification,
            price_verification=case.price_verification,
            fraud_score=case.fraud_score,
            sentiment_level=case.sentiment_level,
            order_reasons=case.order_verification_reasons or [],
            product_reasons=case.product_verification_reasons or [],
            price_reasons=case.price_verification_reasons or [],
            fraud_reasons=fraud_output.get("rules") or [],
            sentiment_keywords=sentiment_output.get("keywords") or [],
        ),
        order_verification=case.order_verification,
        order_verify_reasons=case.order_verification_reasons or [],
        product_verification=case.product_verification,
        product_verify_reasons=case.product_verification_reasons or [],
        price_verification=case.price_verification,
        price_verify_reasons=case.price_verification_reasons or [],
        refund_transactions=[
            RefundTx(refund_id=tx.refund_id, status=tx.status, amount=tx.amount,
                     error=tx.error, created_at=tx.created_at)
            for tx in txs
        ],
        eval_run_id=case.eval_run_id, eval_case_id=case.eval_case_id,
        assignee_id=case.assignee_id,
        approval_opinion=case.approval_opinion, created_at=case.created_at,
        agents=[
            AgentTrace(agent_name=t.agent_name, status=t.status, duration_ms=t.duration_ms,
                       error_message=t.error_message, step_order=t.step_order,
                       output=t.output_snapshot,
                       trace_id=t.trace_id, model=t.model,
                       tools=(t.trace or {}).get("tools") if t.trace else None)
            for t in traces
        ],
    )


@router.get("/{case_id}/evidence")
def get_case_evidence(
    case_id: str,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """返回主管审批所需的原始订单凭证（鉴权接口，不公开 uploads 目录）。"""
    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")

    evidence = (
        db.query(Evidence)
        .filter_by(case_id=case_id)
        .order_by(Evidence.created_at.desc())
        .first()
    )
    if evidence is None:
        raise HTTPException(status_code=404, detail="该案件没有订单凭证")

    # 数据库只保存相对标识；取 basename 可同时兼容历史的 /uploads/x.jpg，
    # 并阻断 ../ 等目录穿越。最终路径还要再次确认位于上传根目录内。
    evidence_path = _resolve_evidence_path(evidence)

    media_type = mimetypes.guess_type(evidence_path.name)[0] or "application/octet-stream"
    return FileResponse(
        evidence_path,
        media_type=media_type,
        filename=evidence_path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{case_id}/evidences")
def list_case_evidences(
    case_id: str,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """返回全部附件及逐张分析结果；不暴露服务器文件路径。"""
    if db.get(RefundCase, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    rows = (
        db.query(Evidence).filter_by(case_id=case_id)
        .order_by(Evidence.created_at, Evidence.id).all()
    )
    return {
        "items": [
            {
                "evidence_id": row.id,
                "file_name": _evidence_original_name(row),
                "ocr_status": row.ocr_status,
                "analysis": normalize_stored_evidence_item(
                    row.ocr_result, _evidence_original_name(row)
                ),
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/{case_id}/evidences/{evidence_id}")
def get_case_evidence_by_id(
    case_id: str,
    evidence_id: str,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    evidence = db.query(Evidence).filter_by(id=evidence_id, case_id=case_id).first()
    if evidence is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    evidence_path = _resolve_evidence_path(evidence)
    media_type = mimetypes.guess_type(evidence_path.name)[0] or "application/octet-stream"
    return FileResponse(
        evidence_path,
        media_type=media_type,
        filename=_evidence_original_name(evidence),
        headers={"Cache-Control": "private, no-store"},
    )


def _decide(case_id: str, action: str, opinion: str, approver: User, db: Session,
            idem_key: str | None = None) -> dict:
    """审批/拒绝公共逻辑（唤醒图模型，docs/06 ）。

    主管审批 = Redis 分布式排他锁 + 写 human_interventions + XADD 决策流
    -> Worker resume 挂起图执行退款；案件保持 SUSPENDED 直到 Worker 完成终态。
    三道并发防线（docs/07 手册 ）：
      1) Redis 分布式锁（SET NX EX + Lua 释放）——防双主管同时审批同一案件；
      2) 已有审批记录 409——不同幂等键的并发审批；
      3) 状态/决策前置校验——已终态或非挂起一律 409。
    """
    token = str(uuid.uuid4())
    if not _approve_lock(case_id, token):
        raise HTTPException(status_code=409, detail="该案件正在被审批，请勿并发操作")
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")

        # 审批前置条件：必须已挂起且决策待定（docs/06 E-12；RUNNING 案件不可审批）
        if case.current_status != CASE_STATUS_SUSPENDED:
            raise HTTPException(status_code=409, detail="案件未处于挂起状态，无法审批")
        if case.final_decision != DECISION_PENDING:
            raise HTTPException(status_code=409, detail="案件已有终态决策，无法审批")

        # 防重复审批：已有待处理决策记录（不同幂等键的并发审批）-> 409
        existing = db.query(HumanIntervention).filter_by(case_id=case_id).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="该案件已有审批记录，请勿重复审批")

        db.add(HumanIntervention(case_id=case_id, approver_id=approver.id, action=action,
                                 opinion=opinion))
        db.execute(
            update(RefundCase)
            .where(RefundCase.id == case_id)
            .values(assignee_id=approver.id, approval_opinion=opinion)
        )
        db.commit()

        # Phase O（O01）：审批链路必须延续案件的原始 trace_id（申请流建单时生成，
        # 案件行 trace_id 已存）——不得再新造 uuid，否则贯穿轨迹在 human_review 断点断裂。
        resume_trace_id = case.trace_id or str(uuid.uuid4())
        xadd(STREAM_HUMAN_DECISION, {"case_id": case_id, "action": action, "opinion": opinion,
                                     "trace_id": resume_trace_id})
        payload = {"case_id": case_id, "current_status": "SUSPENDED",
                   "final_decision": "PENDING", "action": action}
        if idem_key:
            _idempotency_cache_result(idem_key, payload)
        return payload
    finally:
        _approve_unlock(case_id, token)


@router.post("/{case_id}/approve", status_code=202)
def approve_case(
    case_id: str,
    opinion: str = Form(min_length=1, max_length=500),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    supervisor: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    if not x_idempotency_key:
        raise HTTPException(status_code=400, detail="缺少 X-Idempotency-Key")
    existing = _idempotency_claim(x_idempotency_key, case_id=case_id)
    if existing is not None and existing != case_id:
        raise HTTPException(status_code=409, detail="幂等键已用于其他工单")
    cached = _idempotency_get_cached(x_idempotency_key)
    if cached:
        return cached  # 幂等：返回第一次的审批结果
    return _decide(case_id, "APPROVED", opinion, supervisor, db, x_idempotency_key)


@router.post("/{case_id}/reject", status_code=202)
def reject_case(
    case_id: str,
    opinion: str = Form(min_length=1, max_length=500),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    supervisor: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    if not x_idempotency_key:
        raise HTTPException(status_code=400, detail="缺少 X-Idempotency-Key")
    existing = _idempotency_claim(x_idempotency_key, case_id=case_id)
    if existing is not None and existing != case_id:
        raise HTTPException(status_code=409, detail="幂等键已用于其他工单")
    cached = _idempotency_get_cached(x_idempotency_key)
    if cached:
        return cached
    return _decide(case_id, "REJECTED", opinion, supervisor, db, x_idempotency_key)


# ---- 基线 V1.0：HTTP 挂起/恢复（供人工验证 Redis Checkpointer）----

@router.post("/{case_id}/checkpoint", status_code=202)
def checkpoint_case(
    case_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """手动挂起：RUNNING -> SUSPENDED（对齐 runner._mark_suspended）。

    供人工验证挂起语义：把报案单标记 SUSPENDED（决策待定），并确保 Redis
    检查点存在以便 resume。仅 RUNNING 案件可挂起；已终态/已挂起拒绝。
    """
    from app.workflow.runner import WorkflowRunner

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    # 幂等占位（防并发重复 checkpoint）
    if case.current_status != CASE_STATUS_RUNNING:
        raise HTTPException(status_code=409,
                            detail=f"案件当前状态 {case.current_status} 不可手动挂起（仅 RUNNING）")
    db.commit()

    runner = WorkflowRunner()
    try:
        # 若图已跑到 human_review（检查点已存在）直接复用；否则先记录挂起理由
        checkpoint_id = runner.saver.get_tuple({"configurable": {"thread_id": case_id}})
        if checkpoint_id is None:
            # 无检查点（纯 RUNNING 未跑图）-> 落库 SUSPENDED，同时初始化一次安全检查点占位
            runner._mark_suspended(case_id)
        case2 = db.get(RefundCase, case_id)
        if case2 is None:
            raise HTTPException(status_code=404, detail="案件不存在")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"checkpoint 失败: {exc}")
    return {"case_id": case_id, "status": "SUSPENDED", "final_decision": case2.final_decision}


@router.post("/{case_id}/resume", status_code=202)
def resume_case(
    case_id: str,
    action: str = Form(default="APPROVED", pattern="^(APPROVED|REJECTED)$"),
    opinion: str = Form(default="", max_length=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """HTTP 恢复挂起图（复用 runner.resume + RedisHashCheckpointer）。

    仅 SUSPENDED 且决策 PENDING 可恢复；终态/非挂起拒绝。返回恢复后的状态。
    """
    from app.workflow.runner import WorkflowRunner

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    if case.current_status != CASE_STATUS_SUSPENDED:
        raise HTTPException(status_code=409, detail=f"案件当前状态 {case.current_status} 非挂起，无法恢复")
    if case.final_decision != DECISION_PENDING:
        raise HTTPException(status_code=409, detail="案件已有终态决策，无法恢复")
    db.commit()

    runner = WorkflowRunner()
    try:
        outcome = runner.resume(case_id, action, opinion)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"resume 失败: {exc}")
    return {"case_id": case_id, "action": action, "outcome": outcome}


# ---- 基线 V2.0（退款状态产品化）----

@router.get("/{case_id}/refund-status", response_model=RefundStatusResponse)
def get_refund_status(
    case_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """退款状态独立查询（含事件轨迹/回执/重试审计入口）。

    -1：退款状态从单字段升级为独立事件流，供主管/审计按案件追溯每次
    NONE/PENDING/SUCCESS/FAILED 迁移（含 gateway_before 与 guid）。
    """
    from app.services.refund_events import query_refund_status

    if db.get(RefundCase, case_id) is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    return query_refund_status(db, case_id)


@router.get("/{case_id}/order-verify", response_model=OrderVerifyResponse)
def order_verify(
    case_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """订单中心沙箱查询（存在性/归属/交易状态/可退资格）快照。

    -1：每次查询落不可变 `order_lookup_snapshots`，返回最近一次快照；
    若从无查询则现场触发一次并落库（used as serve）。超时/不存在/归属不符等
    以结构化 `reason_codes` 返回，绝不自动改案件状态。
    """
    from app.services.order_lookup import LOOKUP_KIND_API, snapshot_order_lookup

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    snap = snapshot_order_lookup(db, case=case, lookup_kind=LOOKUP_KIND_API)
    item = OrderLookupItem(
        id=snap.id, order_id=snap.order_id,
        exists=snap.exists, owner_locked=snap.owner_locked,
        trade_status=snap.trade_status, refundable=snap.refundable,
        lookup_kind=snap.lookup_kind, gate_result=snap.gate_result,
        reason_codes=snap.reason_codes or [],
        raw_sha256=snap.raw_sha256,
        created_at=snap.created_at.isoformat() if snap.created_at else None,
    )
    return OrderVerifyResponse(case_id=case_id, served=True, snapshot=item)


@router.get("/{case_id}/checkpoint-view", response_model=CheckpointViewResponse)
def checkpoint_view(
    case_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """挂起检查点只读查看（不允许篡改序列化上下文）。

    只返回 next 节点名 + 检查点存在性（不含图上下文/敏感字段）。
    """
    from app.workflow.redis_checkpointer import RedisHashCheckpointer

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")

    saver = RedisHashCheckpointer()
    tup = saver.get_tuple({"configurable": {"thread_id": case_id}})
    next_nodes: list[str] = []
    checkpoint_id = None
    has = bool(tup and tup.checkpoint and tup.checkpoint.get("id"))
    if has:
        checkpoint_id = tup.checkpoint["id"]
        # snapshot.next 在 metadata/channel 中；安全提取节点名（不展开上下文）
        try:
            from langgraph.checkpoint.serde.base import SerdeError  # noqa: F401

            nxt = (tup.checkpoint.get("channel_values") or {}).get("__node_stack__") or []
            next_nodes = [str(x) for x in nxt][:10]
        except Exception:  # noqa: BLE001 —— 只读降级
            next_nodes = []
    return CheckpointViewResponse(
        case_id=case_id, has_checkpoint=has, checkpoint_id=checkpoint_id,
        status=case.current_status, final_decision=case.final_decision,
        next_nodes=next_nodes,
    )


@router.post("/{case_id}/checkpoint-recover", status_code=202)
def checkpoint_recover(
    case_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """受控恢复：从 Redis Checkpointer 恢复挂起图（复用 runner.resume）。

    -1：主管确认后可恢复挂起案件；仅 SUSPENDED+PENDING 可恢复，
    终态拒绝。恢复后图自动走完剩余节点并落退款事件流（幂等）。
    """
    from app.workflow.runner import WorkflowRunner

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    if case.current_status != CASE_STATUS_SUSPENDED:
        raise HTTPException(status_code=409, detail="案件未挂起，无法恢复检查点")
    if case.final_decision != DECISION_PENDING:
        raise HTTPException(status_code=409, detail="案件已有终态决策，无法恢复")
    saver = WorkflowRunner().saver
    if not saver.get_tuple({"configurable": {"thread_id": case_id}}):
        raise HTTPException(status_code=409, detail="无可用检查点（可能已过期），请转人工处理")
    db.commit()
    runner = WorkflowRunner()
    try:
        outcome = runner.resume(case_id, "APPROVED", "检查点受控恢复")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"checkpoint 恢复失败: {exc}")
    return {"case_id": case_id, "action": "recover", "outcome": outcome}


@router.put("/{case_id}/reason-confirm", response_model=RefundStatusResponse)
def reason_confirm(
    case_id: str,
    body: ReasonConfirmRequest,
    supervisor: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    """主管确认/驳回/补充材料（结构化原因码）。

    -1 reason-confirm：主管对退款状态的确认。仅 APPROVED 允许指定
    refund_status=SUCCESS（退款终态确认）；REJECTED => refund_status=FAILED。
    每次确认落一条 refund_events(CONFIRM) 审计，返回最新事件轨迹。
    """
    from app.domain.models import DECISION_REJECTED
    from app.services.refund_events import confirm_refund_event, query_refund_status

    case = db.get(RefundCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")

    action = (body.action or "").upper()
    if action not in ("APPROVED", "REJECTED", "SUPPLEMENT"):
        raise HTTPException(status_code=400, detail="action 仅支持 APPROVED/REJECTED/SUPPLEMENT")

    if action == "APPROVED":
        new_status = body.refund_status or case.refund_status or "SUCCESS"
    elif action == "REJECTED":
        new_status = "FAILED"
    else:  # SUPPLEMENT：补充材料不改变退款状态，仅留审计
        try:
            confirm_refund_event(
                db, case_id=case_id, new_status=case.refund_status or "NONE",
                opinion=body.opinion, confirmed_by=supervisor.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return query_refund_status(db, case_id)

    try:
        confirm_refund_event(
            db, case_id=case_id, new_status=new_status,
            opinion=f"supervisor {action} confirm: {body.opinion}",
            confirmed_by=supervisor.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return query_refund_status(db, case_id)
