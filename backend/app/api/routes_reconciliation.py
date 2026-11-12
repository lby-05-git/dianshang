# -*- coding: utf-8 -*-
"""订单/退款一致性对账 API（V3.0 ）。

`/api/v1/reconciliation/*`（主管+）：
  - GET  /overview                   对账大盘（差异分级/类别/未处置告警/最近差异）
  - POST /run                        触发生成对账差异（幂等，重跑不重复落审计）
  - GET  /metrics                    对账度量（核验失败率/退款成功率/重试/不一致率）
  - GET  /diffs                      按 case/severity 过滤历史差异（审计）
  - GET  /alerts                     未处置的 HIGH+ 差异（告警）
  - POST /alerts/{event_id}/resolve  主管处置（只置位，不删审计行）
  - GET  /sources                    多来源订单核验聚合状态（含熔断）
  - GET  /cases/{case_id}/source-verdicts  单案各来源判定留痕

红线：全程只读判定 + 只写审计，绝不改任何退款/评测/安全判定。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, require_min_role
from app.domain.models import RefundCase, User
from app.infrastructure.db import get_db
from app.services import reconciliation as recon

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


class RunReconciliationBody(BaseModel):
    case_id: str | None = None
    window_minutes: int | None = None


@router.get("/overview")
def overview(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
             db: Session = Depends(get_db),
             window_minutes: int | None = Query(default=None, ge=1, le=10080)):
    return {"item": recon.overview(db, window_minutes=window_minutes)}


@router.get("/metrics")
def metrics(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
            db: Session = Depends(get_db),
            window_minutes: int | None = Query(default=None, ge=1, le=10080)):
    return {"item": recon.reconciliation_metrics(db, window_minutes=window_minutes)}


@router.post("/run", status_code=202)
def run_reconciliation(body: RunReconciliationBody,
                       user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                       db: Session = Depends(get_db)):
    """触发一轮对账。幂等：同 run 同案同差异 guid 唯一，重跑不重复落审计。"""
    if body.case_id:
        case = db.get(RefundCase, body.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
    try:
        out = recon.run_reconciliation(db, run_id=None, case_id=body.case_id,
                                       window_minutes=body.window_minutes,
                                       actor_id=str(user.id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"item": out}


@router.get("/diffs")
def diffs_list(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
               db: Session = Depends(get_db),
               case_id: str | None = None,
               severity: str | None = Query(default=None,
                                            pattern="^(INFO|WARNING|HIGH|CRITICAL)$"),
               unresolved_only: bool = False,
               limit: int = Query(default=50, ge=1, le=500)):
    return {"items": recon.list_diffs(db, case_id=case_id, severity=severity,
                                      unresolved_only=unresolved_only, limit=limit)}


@router.get("/alerts")
def alerts_list(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                db: Session = Depends(get_db),
                unresolved_only: bool = True,
                limit: int = Query(default=50, ge=1, le=500)):
    rows = recon.list_diffs(db, unresolved_only=unresolved_only, limit=limit)
    # 告警 = HIGH+ 差异（与 overview.unresolved_alerts 口径一致）
    alerts = [r for r in rows if r["severity"] in ("HIGH", "CRITICAL")]
    return {"items": alerts}


@router.post("/alerts/{event_id}/resolve")
def alerts_resolve(event_id: str,
                   user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                   db: Session = Depends(get_db)):
    out = recon.resolve_alert(db, event_id, actor_id=str(user.id))
    if out is None:
        raise HTTPException(status_code=404, detail="差异不存在")
    return {"item": out}


@router.get("/sources")
def sources_state(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                  db: Session = Depends(get_db)):
    """多来源订单核验聚合状态（默认来源注册 + 熔断器状态 + 信任权重）。"""
    try:
        from app.infrastructure.order_provider import get_multi_source_provider
        mp = get_multi_source_provider()
        return {"item": {
            "names": mp.source_names,
            "trust": {n: mp._trust[n] for n in mp.source_names},
            "breakers": mp.source_state(),
        }}
    except Exception:  # noqa: BLE001 —— 聚合源未配置时返回空状态
        return {"item": {"names": [], "trust": {}, "breakers": {}, "note": "未配置多来源聚合"}}