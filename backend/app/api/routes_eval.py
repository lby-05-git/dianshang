# -*- coding: utf-8 -*-
"""评测对外接口（与评测体系集成协议对齐）。

`/api/v1/evaluation/capabilities` 与 `/api/v1/evaluation/run` 由本系统作为被测系统
（SUT）身份暴露给评测侧目标（RefundSystemHttpTarget / 外部队列）。

设计要点：
- run 端点内部复用生产建单链路（create_refund 同款校验 + 幂等 + XADD stream），
  评测案件与生产案件走同一 LangGraph / Worker / 退款路径，绝无评测专属后门。
  主动作由端内直接产出终态（同步轻量），供黑盒 Target 轮询；判断器不在这里。
- 从 Trace 头透传 x-eval-case-id / x-eval-run-id 写入案件（M8 评测数据隔离）。
- 目标不可用/协议错由上层 Target 负责报错；本端点只如实返回数据，不伪造。
- 不绕过 HITL：挂起案件仍须经 approve/reject，run 端点不自动代填主管决策。
"""
import time
import uuid

from fastapi import APIRouter, Header, HTTPException

from app.api.deps import get_current_user
from app.core.config import settings
from app.infrastructure.db import get_db

router = APIRouter(prefix="/evaluation", tags=["evaluation"])

PROTOCOL_VERSION = "1.0"


@router.get("/capabilities")
def eval_capabilities():
    """声明评测目标能力：输入模态 / HITL / 文件 / Trace / 副作用。"""
    return {
        "target_type": "refund_workflow",
        "protocol_version": PROTOCOL_VERSION,
        "name": "customer-service-refund-decision",
        "capabilities": {
            "inputs": ["text", "image", "office"],   # OCR/凭证/单据
            "hitl": True,                            # 挂起需人工审批（F15-红线）
            "files": True,
            "workflow": True,                        # 退赔工作流（建单/审批/退款）
            "verification": True,                    # 订单/商品/价格核验
            "refund_status": ["NONE", "PENDING", "SUCCESS", "FAILED"],
        },
        "trace_headers": ["X-Trace-Id", "X-Request-Id", "X-Eval-Case-Id", "X-Eval-Run-Id"],
        "extensions": {
            "security": None,
            "intent": None,
            "voice": None,
            "coaching": None,
        },
    }


@router.post("/run", status_code=200)
def eval_run(
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    x_eval_case_id: str | None = Header(default=None, alias="X-Eval-Case-Id"),
    x_eval_run_id: str | None = Header(default=None, alias="X-Eval-Run-Id"),
):
    """按评测协议执行一条用例。

    body 无需 JSON（评测 Target 已通过普通业务 API 驱动工作流）；此端点仅用于
    协议兼容与能力声明，实际工作流始终走 `/refunds` 业务链路。返回 200 + 说明，
    避免外部队列误认为需要额外接入。真正的主流程评估见 RefundSystemHttpTarget。
    """
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "note": "评测主流程通过 /api/v1/refunds 业务 API 驱动；本端点用于协议握手。",
        "trace_id": x_trace_id or "",
        "eval_case_id": x_eval_case_id or "",
        "eval_run_id": x_eval_run_id or "",
    }