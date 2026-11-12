# -*- coding: utf-8 -*-
"""退款适配器（docs/08 第7阶段：退款动作必须有独立幂等键）。

无真实支付凭证 -> Mock 网关；退款幂等：SETNX refund:{case_id} 唯一流水号，
Worker 重投/重试时同一案件只执行一次退款，绝不重复退钱（防资损核心，docs/07 手册 ）。

每次退款落一条 refund_transactions 回执（3.3.6），并同步 RefundCase.refund_status。
"""
import logging
import time
import uuid
from decimal import Decimal

from app.domain.models import (
    REFUND_STATUS_FAILED,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    RefundTransaction,
)
from app.infrastructure.db import SessionLocal
from app.infrastructure.redis_client import redis_client

logger = logging.getLogger("infra.refund")

# V2.0 事件流（-1）：延迟导入避免循环依赖（refund_events -> models）
def _record_event(db, *, case_id: str, refund_id: str | None, status: str,
                  error: str | None, previous_status: str) -> None:
    """退款事件流同事务留痕（guid 幂等，重放不重复）。"""
    try:
        from app.services.refund_events import record_refund_event

        guid = f"re-{refund_id}" if refund_id else None
        record_refund_event(
            db, case_id=case_id,
            event_type="OK" if status == REFUND_STATUS_SUCCESS else
                        ("FAIL" if status == REFUND_STATUS_FAILED else "EXEC"),
            previous_status=previous_status or "NONE",
            new_status=status,
            guid=guid,
            reason=error or (f"refund executed {refund_id}" if refund_id else "exec"),
            set_case_status=False,   # 状态已由调用方更新（避免重复写）
        )
    except Exception:  # noqa: BLE001 —— 事件流失败不阻断退款（审计尽力而为）
        logger.exception("record refund_event failed: case=%s", case_id)

REFUND_IDEM_TTL = 86400  # 24h


class MockRefundProvider:
    """本地 Mock 网关：不调用真实支付系统；唯一流水号 + SETNX 幂等。"""

    def __init__(self, fail_once: bool = False):
        self._fail_once = fail_once
        self.last_refund_id: str | None = None

    def execute(self, case_id: str, amount: Decimal) -> tuple[bool, str | None]:
        """执行退款，返回 (ok, error)。幂等：同案件只产生一个退款流水号。

        成功后会向 refund_transactions 落一条回执（PENDING -> SUCCESS），
        失败保留一条 FAILED 记录供追溯（3.3.6）。
        """
        # 独立幂等键（退款流水号）：已退过 -> 直接返回既有流水号，不重复执行
        refund_id = self._claim(case_id)
        if refund_id is not None:
            self.last_refund_id = refund_id
            # 幂等跳过：仅确保回执状态兜底为 SUCCESS
            self._record(case_id, refund_id, amount, REFUND_STATUS_SUCCESS, None)
            logger.info("refund idempotent skip: case=%s refund_id=%s", case_id, refund_id)
            return True, None

        if self._fail_once:
            self._fail_once = False
            err = "mock gateway timeout (injected)"
            # 已成功退过（幂等键存在）则绝不以失败覆盖已到账状态（状态机不回滚）
            if self._claim(case_id):
                return True, None
            self._record(case_id, None, amount, REFUND_STATUS_FAILED, err)
            return False, err

        time.sleep(0.1)  # 模拟网关耗时
        refund_id = f"RF-{uuid.uuid4().hex[:12]}"
        if redis_client.set(f"refund:{case_id}", refund_id, nx=True, ex=REFUND_IDEM_TTL):
            self.last_refund_id = refund_id
            self._record(case_id, refund_id, amount, REFUND_STATUS_SUCCESS, None)
            logger.info("mock refund executed: case=%s amount=%s refund_id=%s",
                        case_id, amount, refund_id)
            return True, None
        self.last_refund_id = redis_client.get(f"refund:{case_id}")
        self._record(case_id, self.last_refund_id, amount, REFUND_STATUS_SUCCESS, None)
        return True, None

    def _claim(self, case_id: str) -> str | None:
        """返回已存在的退款流水号；不存在返回 None。"""
        return redis_client.get(f"refund:{case_id}")

    def _record(
        self,
        case_id: str,
        refund_id: str | None,
        amount: Decimal,
        status: str,
        error: str | None,
    ) -> None:
        """落一条退款回执 + 同步案件退款状态（回执幂等、状态机不回滚）。

        幂等/审计：同一流水号（case_id, refund_id）只保留一条回执。
        幂等 skip / 重试重复执行时，更新既有回执的状态与错误，绝不重复 INSERT，
        避免 refund_transactions 无限累积、审计上无法区分真实执行与重投。
        失败回执（refund_id 为空、error 非空）每次都 INSERT——它们是唯一流水号
        产生前的失败记录，多条 FAILED 用于失败重试的完整审计轨迹。
        """
        if not error and not refund_id:
            refund_id = f"RF-{uuid.uuid4().hex[:12]}"
        db = SessionLocal()
        try:
            existing = None
            if refund_id:
                existing = (
                    db.query(RefundTransaction)
                    .filter_by(case_id=case_id, refund_id=refund_id)
                    .order_by(RefundTransaction.created_at.desc())
                    .first()
                )
            if existing is not None:
                # 幂等：只更新既有回执（状态/错误/金额），不新增
                existing.status = status
                existing.error = error
                existing.amount = amount
            else:
                db.add(RefundTransaction(
                    case_id=case_id,
                    refund_id=refund_id,
                    status=status,
                    amount=amount,
                    error=error,
                ))
            case = db.get(RefundCase, case_id)
            prev_status = case.refund_status if case is not None else None
            if case is not None:
                case.refund_status = status
            # 基线 V2.0（-1）：退款事件流 add-only 审计。
            # 回执落库与事件流同事务，保证"到账即留痕"，重放不重复。
            _record_event(db, case_id=case_id, refund_id=refund_id, status=status,
                          error=error, previous_status=prev_status)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("persist refund receipt failed: case=%s", case_id)
        finally:
            db.close()
