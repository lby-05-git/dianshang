# -*- coding: utf-8 -*-
"""工作流执行器：启动图 / 恢复图（挂起序列化 + 唤醒）。

核心机制（docs/07 手册 ）：
- RedisSaver Checkpointer：thread_id = case_id，图上下文序列化进
  langgraph:checkpoint:{case_id}，任何 Worker 可凭 case_id 恢复（无状态服务）；
- start()：若返回时图还有未执行节点（snapshot.next 非空）→ 挂起 → 写 SUSPENDED；
- resume()：Command(resume={action, opinion}) 从 interrupt 处继续执行。
"""
import logging

from langgraph.types import Command

from app.domain.models import (
    CASE_STATUS_SUSPENDED,
    DECISION_PENDING,
    RefundCase,
)
from app.domain.sla import compute_sla_deadline
from app.domain.state_machine import transition_case
from app.infrastructure.audit import write_event
from app.infrastructure.db import SessionLocal
from app.infrastructure.redis_client import redis_client
from app.services.policy_service import resolve_policy_for_case
from app.workflow.graph import build_graph
from app.workflow.redis_checkpointer import RedisHashCheckpointer

logger = logging.getLogger("workflow.runner")

# SLA 默认升级时效（小时）：与 default 策略档 escalate_hr 兜底对齐（D4）
SLA_ESCALATE_HR = 24


def _config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


class WorkflowRunner:
    def __init__(self):
        # 自研轻量 Redis Checkpointer（Redis 5.0.14 无 RediSearch，redisvl 方案不可用；
        # 键设计 langgraph:checkpoint:{case_id} 见 docs/07 手册 ）
        self.saver = RedisHashCheckpointer()
        self.graph = build_graph().compile(checkpointer=self.saver)

    # ---- 启动新图（Worker 消费申请流时调用）----
    def start(self, case_id: str, trace_id: str | None = None) -> str:
        initial = {"case_id": case_id, "trace_id": trace_id or case_id,
                   "amount_cents": 0, "ocr_confidence": 0.0, "fraud_score": 0,
                   "sentiment_level": "LOW", "decision": "PENDING", "errors": []}
        config = _config(case_id)
        try:
            self.graph.invoke(initial, config=config)
        except Exception as exc: # noqa: BLE001 —— ：检查点/引擎异常 -> 安全失败转人工
            logger.exception("graph start failed for case=%s: %s", case_id, exc)
            try:
                self._mark_suspended(case_id)
            except Exception:  # noqa: BLE001 —— 标记失败不掩盖原因
                pass
            return "SUSPENDED"
        snapshot = self.graph.get_state(config)
        if snapshot.next:  # 有未执行节点 -> interrupt 挂起
            self._mark_suspended(case_id)
            return "SUSPENDED"
        # ：终态案件跑完即清理检查点（图上下文不再需要），
        # 配合挂起案件的 TTL 兜底，防止 langgraph:checkpoint:* 无限膨胀。
        self.saver.delete(case_id)
        return "COMPLETED"

    # ---- 恢复挂起图（Worker 消费人工决策流时调用）----
    def resume(self, case_id: str, action: str, opinion: str) -> str:
        config = _config(case_id)
        # ：检查点缺失/损坏/Redis 不可用时，不得从空状态错误恢复。
        # 先探测检查点可读性与结构；不可用则安全失败（保持挂起，人工线下处置），
        # 绝不从空态 start 误判为可恢复图。
        if not self._checkpoint_usable(case_id):
            logger.error("resume refused for %s: checkpoint unavailable (missing/corrupt/redis-down)",
                         case_id)
            return "SUSPENDED"
        self.graph.invoke(
            Command(resume={"action": action, "opinion": opinion}),
            config=config,
        )
        snapshot = self.graph.get_state(config)
        if snapshot.next:
            self._mark_suspended(case_id)
            return "SUSPENDED"
        self.saver.delete(case_id)
        return "COMPLETED"

    # ---- 检查点可用性探测 ----
    def _checkpoint_usable(self, case_id: str) -> bool:
        """latest 字段存在且对应检查点可反序列化、结构合法，才视为可用。"""
        try:
            tup = self.saver.get_tuple(_config(case_id))
            return bool(tup and tup.checkpoint and tup.checkpoint.get("id"))
        except Exception:  # noqa: BLE001 —— Redis 断连/引擎异常一律视为不可用
            logger.exception("checkpoint probe failed for case=%s", case_id)
            return False

    # ---- 挂起落库（状态机校验：RUNNING/SUSPENDED -> SUSPENDED）----
    def _mark_suspended(self, case_id: str) -> None:
        db = SessionLocal()
        try:
            case = db.get(RefundCase, case_id)
            if case is None or case.final_decision != DECISION_PENDING:
                return
            ns, nd = transition_case(
                current_status=case.current_status,
                current_decision=case.final_decision,
                new_status=CASE_STATUS_SUSPENDED,
            )
            case.current_status = ns
            case.final_decision = nd
            # SLA 启时钟（D4）：挂起那一刻启动 escalate 小时倒计时；只设时点，不自动退款。
            # 时效取自该案件渠道生效的策略档（PolicyCenter 可配），无则回退默认 24h。
            if case.sla_deadline_at is None:
                policy = resolve_policy_for_case(db, case)
                escalate_hr = int(policy.get("sla_escalate_hr") or SLA_ESCALATE_HR)
                case.sla_deadline_at = compute_sla_deadline(escalate_hr=escalate_hr)
            db.flush()
            # 挂起/升级为领域事件留痕（append-only，3.3.7）
            write_event(db, actor_id=case.assignee_id, case_id=case_id,
                        action="CASE_SUSPENDED",
                        payload={"current_status": ns, "final_decision": nd,
                                 "sla_deadline_at": str(case.sla_deadline_at)})
            db.commit()
            logger.info("case %s suspended (graph interrupted), sla_deadline=%s",
                        case_id, case.sla_deadline_at)
        finally:
            db.close()
