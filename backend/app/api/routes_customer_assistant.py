# -*- coding: utf-8 -*-
"""商城第一阶段只读 RAG 知识客服 API。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.domain.models import GATE_PASS, User
from app.infrastructure.db import get_db
from app.services.rag_service import answer_question
from app.services.security_gateway import gate_text


router = APIRouter(prefix="/customer-assistant", tags=["customer-assistant"])


class CatalogDocument(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    answer: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    catalog_documents: list[CatalogDocument] = Field(default_factory=list, max_length=12)


@router.post("/chat")
def chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """安全判定后执行只读检索；本路由没有订单/退款写工具。"""
    security = gate_text(
        db, text=body.question, source="customer_assistant",
        tool_name="knowledge_search", persist=True,
        # 使用既有最严格沙箱场景策略：商城问答不需要保留任何个人敏感字段。
        ner=True, role="customer", scenario="sandbox",
    )
    if security.gate_result != GATE_PASS or security.findings:
        reason = "检测到可能的提示词注入" if security.gate_result != GATE_PASS else "检测到敏感信息"
        return {
            "answer": f"为保护账户与系统安全，本次问题已拦截：{reason}。请删除敏感内容后重新提问。",
            "uncertain": False,
            "confidence": None,
            "sources": [],
            "safety": {
                "blocked": True,
                "reason": "PROMPT_INJECTION" if security.gate_result != GATE_PASS else "SENSITIVE_INFORMATION",
                "risk_level": security.risk_level,
            },
            "capabilities": {"read_only": True, "order_write": False, "refund_execute": False},
        }

    result = answer_question(
        security.masked_input,
        catalog_documents=[item.model_dump() for item in body.catalog_documents],
    )
    boundary_blocked = bool(result.pop("boundary_blocked", False))
    return {
        **result,
        "safety": {
            "blocked": boundary_blocked,
            "reason": "READ_ONLY_BOUNDARY" if boundary_blocked else None,
            "risk_level": security.risk_level,
        },
        "capabilities": {"read_only": True, "order_write": False, "refund_execute": False},
    }
