# -*- coding: utf-8 -*-
"""输入安全网关端点（基线 V1.0）。

- POST /api/v1/security/critic-check    输入安全网关：注入检测 + DLP 脱敏
- POST /api/v1/security/dlp-mask        单开 DLP 脱敏（4 类正则）
- GET  /api/v1/security/findings/{case_id}  查某案件 DLP findings
- POST /api/v1/security/tool-gate       退款工具前置策略校验（内部用/前端预检）

全部只读/记录；BLOCK/REVIEW 决策由调用方执行，本层不改业务状态。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.domain.models import User
from app.infrastructure.db import get_db
from app.services.security_gateway import diagnose, gate_text, list_findings, mask_dlp
from app.services.tool_gate import check_refund_gate

router = APIRouter(prefix="/security", tags=["security"])


class SecurityCheckRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class SecurityCheckResponse(BaseModel):
    gate_result: str
    injection_score: float
    risk_level: str
    masked_input: str
    rule_hits: list[dict]
    findings: list[dict]


@router.post("/critic-check", response_model=SecurityCheckResponse)
def critic_check(
    payload: SecurityCheckRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """注入检测 + DLP 脱敏（纯判定，落审计）。"""
    res = gate_text(db, text=payload.text, source="critic", persist=True)
    return SecurityCheckResponse(
        gate_result=res.gate_result,
        injection_score=res.injection_score,
        risk_level=res.risk_level,
        masked_input=res.masked_input,
        rule_hits=[{"rule": n, "score": w} for n, w in res.rule_hits],
        findings=res.findings,
    )


class DlpMaskResponse(BaseModel):
    masked_text: str
    findings: list[dict]


@router.post("/dlp-mask", response_model=DlpMaskResponse)
def dlp_mask(
    payload: SecurityCheckRequest,
    user: User = Depends(get_current_user),
):
    """单开 DLP 脱敏（4 类正则），不落库。"""
    masked, findings = mask_dlp(payload.text)
    return DlpMaskResponse(masked_text=masked, findings=findings)


@router.get("/findings/{case_id}")
def findings(
    case_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询某案件 DLP findings（只读）。"""
    rows = list_findings(db, case_id)
    return {"case_id": case_id, "total": len(rows), "findings": rows}


class ToolGateRequest(BaseModel):
    case_id: str = Field(min_length=1)


@router.post("/tool-gate")
def tool_gate_check(
    payload: ToolGateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """退款工具前置策略校验（只读预检，不执行退款）。"""
    from app.domain.models import RefundCase

    case = db.get(RefundCase, payload.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="案件不存在")
    dec = check_refund_gate(db, case=case, persist=True)
    return dec.as_dict()