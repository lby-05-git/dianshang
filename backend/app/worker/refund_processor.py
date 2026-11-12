# -*- coding: utf-8 -*-
"""申请流消费者：stream:refund_apply（group:apply）。

消费后启动 LangGraph 工作流（intake -> ocr -> fraud -> sentiment -> 决策 -> 挂起/自动）。
幂等：已终态案件直接跳过（Worker 重启重投 / 重复消息防重）。
"""
import logging
import uuid

from app.domain.models import DECISION_PENDING, RefundCase
from app.infrastructure.db import SessionLocal
from app.infrastructure.redis_client import STREAM_REFUND_APPLY
from app.worker.base import RedisStreamConsumer

logger = logging.getLogger("worker.apply")


class RefundApplyConsumer(RedisStreamConsumer):
    stream = STREAM_REFUND_APPLY
    group = "group:apply"

    def process(self, message_id: str, fields: dict) -> None:
        case_id = fields["case_id"]
        db = SessionLocal()
        try:
            case = db.get(RefundCase, case_id)
            if case is None:
                return
            if case.final_decision != DECISION_PENDING:
                return  # 已终态（重投/已处理）→ 幂等跳过
        finally:
            db.close()

        from app.workflow.runner import WorkflowRunner

        runner = WorkflowRunner()
        outcome = runner.start(case_id, fields.get("trace_id") or str(uuid.uuid4()))
        logger.info("graph start done for case %s -> %s", case_id, outcome)
