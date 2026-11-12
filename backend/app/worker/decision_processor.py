# -*- coding: utf-8 -*-
"""人工决策流消费者：stream:human_decision（group:decision）。

恢复挂起的 LangGraph 图（resume）并继续执行（退款 -> 终态）。
幂等：已终态案件跳过；无挂起图的案件（Phase 3 旧数据）跳过。
"""
import logging

from app.domain.models import DECISION_PENDING, RefundCase
from app.infrastructure.db import SessionLocal
from app.infrastructure.redis_client import STREAM_HUMAN_DECISION
from app.worker.base import RedisStreamConsumer

logger = logging.getLogger("worker.decision")


class HumanDecisionConsumer(RedisStreamConsumer):
    stream = STREAM_HUMAN_DECISION
    group = "group:decision"

    def process(self, message_id: str, fields: dict) -> None:
        case_id = fields["case_id"]
        action = fields.get("action", "REJECTED")
        opinion = fields.get("opinion", "")
        db = SessionLocal()
        try:
            case = db.get(RefundCase, case_id)
            if case is None:
                return
            if case.final_decision != DECISION_PENDING:
                return  # 已终态 -> 幂等跳过
            if case.current_status != "SUSPENDED":
                return  # 无挂起图 -> 跳过
        finally:
            db.close()

        from app.workflow.runner import WorkflowRunner

        runner = WorkflowRunner()
        outcome = runner.resume(case_id, action, opinion)
        logger.info("graph resume done for case %s -> %s", case_id, outcome)
