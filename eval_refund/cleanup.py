# -*- coding: utf-8 -*-
"""评测数据清理（5.2 业务与评测隔离）。

约束（5.2）：
- 只按明确 Run / eval_case_id 标识清理，绝不模糊删除业务工单；
- 只允许删除 eval_run_id 非空的评测打标案件（生产数据永不触碰）；
- 删除前校验归属：仅处理 `eval_run_id = <run_id>` 的行，并返回已删数量供人工复核。
"""

from __future__ import annotations

from typing import Sequence


def cleanup_run(
    session,
    *,
    run_id: str,
    case_ids: Sequence[str] | None = None,
) -> dict:
    """按明确 Run 标识清理评测案件。

    session     数据库会话（SQLAlchemy）
    run_id      要清理的评测运行标识（X-Eval-Run-Id）
    case_ids    可选：仅清理这些 eval_case_id（进一步收窄，避免误删同 run 其他）
    返回 {"deleted": n, "evaluated_deleted": [...]}
    """
    from app.domain.models import RefundCase

    q = session.query(RefundCase).filter(RefundCase.eval_run_id == run_id)
    if case_ids:
        q = q.filter(RefundCase.eval_case_id.in_(case_ids))
    rows = q.all()
    ids = [r.id for r in rows]
    if ids:
        session.query(RefundCase).filter(RefundCase.id.in_(ids)).delete(
            synchronize_session=False)
        session.commit()
    return {"deleted": len(ids), "delete_ids": ids}


def verify_no_pending_refunds_before_cleanup(session, run_id: str) -> bool:
    """清理前安全闸：若该 run 存在已批准但退款仍 PENDING 的案件，禁止清理。"""
    from app.domain.models import RefundCase

    pending = session.query(RefundCase).filter(
        RefundCase.eval_run_id == run_id,
        RefundCase.final_decision.in_(("APPROVED",)),
        RefundCase.refund_status.in_(("PENDING", ""))
    ).count()
    return pending == 0