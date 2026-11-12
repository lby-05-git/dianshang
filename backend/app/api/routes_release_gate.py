# -*- coding: utf-8 -*-
"""发布门禁 + 候选回放（M19 V3.0 持续评测治理）。

`/api/v1/evaluation-center/gate`：
  - GET    /baseline/{suite_id}      当前套件发布基线（Evidence-chain Run）
  - POST   /baseline/{suite_id}      人工批准候选 Run 为基线（COMPLETED 才可；留痕）
  - POST   /evaluate                 对候选 Run 做门禁判定（只读；绝不改任何退款/决策）
  - GET    /results                  历史门禁判定记录（证据）
  - POST   /replay-set               为候选生成回放集（tier_map 索引；不执行、不改数据）
  - GET    /replay-set/{suite_id}/{tier}   回放集 hydration（golden-follower/HTTP 契约）

红线：
  - 门禁/回放只读：不修改候选/基线 Run、不修改数据集、不调用真实退款；
  - MISMATCH/UNKNOWN 触发自动退款 -> 门禁 fail（资金红线）；
  - degraded/skipped/未测量一律不视为通过。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_LEADER, require_min_role
from app.domain.models import EvalSuite, User
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.services import release_gate as rg

router = APIRouter(prefix="/evaluation-center/gate", tags=["evaluation-center"])


class ApproveBaselineBody(BaseModel):
    suite_id: str = Field(min_length=1, max_length=36)
    run_id: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=500)


class EvaluateGateBody(BaseModel):
    run_id: str = Field(min_length=1, max_length=40)
    suite_id: str = Field(min_length=1, max_length=36)
    baseline_run_id: str | None = Field(default=None, max_length=40,
                                        description="缺省用套件当前基线")
    tier: str = Field(default="full", pattern="^(smoke|core|full|security|voice)$")


class ReplaySetBody(BaseModel):
    suite_id: str = Field(min_length=1, max_length=36)
    run_id: str = Field(min_length=1, max_length=40,
                        description="候选 Run（回放集来源证据）")
    tier: str = Field(default="core", pattern="^(smoke|core|full|security|voice)$")


class EvidenceExportBody(BaseModel):
    run_id: str = Field(min_length=1, max_length=40,
                        description="证据链中的 Run（候选/基线）")
    suite_id: str = Field(min_length=1, max_length=36)
    code_ref: str | None = Field(default=None, max_length=128,
                                 description="代码提交 hash/标签（缺省注记 trigger_source）")
    note: str = Field(default="", max_length=500)


@router.get("/baseline/{suite_id}")
def baseline_get(suite_id: str,
                user: User = Depends(require_min_role(ROLE_LEADER)),
                db: Session = Depends(get_db)):
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    bl = rg.get_baseline(db, suite_id)
    return {"item": bl}


@router.post("/baseline", status_code=201)
def baseline_approve(body: ApproveBaselineBody,
                    user: User = Depends(require_min_role(ROLE_LEADER)),
                    db: Session = Depends(get_db)):
    """人工批准候选 Run 为发布基线（approver/approved_at 留痕）。"""
    try:
        item = rg.approve_baseline(db, run_id=body.run_id,
                                   suite_id=body.suite_id,
                                   actor_id=str(user.id), note=body.note)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run 或数据集不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="GATE_BASELINE_APPROVED",
                payload={"suite_id": body.suite_id, "run_id": body.run_id,
                         "note": body.note})
    db.commit()
    return {"item": item}


@router.post("/evaluate")
def gate_evaluate(body: EvaluateGateBody,
                 user: User = Depends(require_min_role(ROLE_LEADER)),
                 db: Session = Depends(get_db)):
    """对候选 Run 做发布门禁判定（只读）。degraded/skipped/未测量不 PASS。"""
    suite = db.get(EvalSuite, body.suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    baseline_run_id = body.baseline_run_id
    if not baseline_run_id:
        bl = rg.get_baseline(db, body.suite_id)
        baseline_run_id = bl.get("run_id") if bl else None
    try:
        res = rg.evaluate_release_gate(
            db, run_id=body.run_id, suite_id=body.suite_id,
            baseline_run_id=baseline_run_id, tier=body.tier)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Run/报告不存在: {exc}")
    # 持久化门禁判定证据（供治理页/审计）
    rg.save_gate_result(db, res, suite_id=body.suite_id,
                        actor_id=str(user.id))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="GATE_EVALUATED",
                payload={"suite_id": body.suite_id, "run_id": body.run_id,
                         "tier": body.tier,
                         "decision": res.as_dict().get("decision"),
                         "passed": res.as_dict().get("passed")})
    db.commit()
    return {"item": res.as_dict()}


@router.get("/results")
def gate_results_list(user: User = Depends(require_min_role(ROLE_LEADER)),
                     db: Session = Depends(get_db)):
    return {"items": rg.list_gate_results(db)}


@router.get("/trusted")
def gate_trusted_health(user: User = Depends(require_min_role(ROLE_LEADER)),
                       db: Session = Depends(get_db)):
    """可信运行积攒门槛驾驶舱（V3.2 ：≥10 次可信评测 + 稳定基线）。

    只读：返回可信运行数 / 按套件分布 / 基线批准状态 / 缺口；
    明确如实地报告未达标（不刷数不造假）。批准动作由
    scripts/accumulate_trusted_runs.py 或定时任务在阈值达标时执行。
    """
    return {"item": rg.trusted_runs_health(db)}


@router.post("/replay-set")
def replay_set_ensure(body: ReplaySetBody,
                     user: User = Depends(require_min_role(ROLE_LEADER)),
                     db: Session = Depends(get_db)):
    """为候选 Run 生成回放集（tier_map 索引；不执行、不修改任何数据）。"""
    try:
        replay = rg.build_replay_set(db, suite_id=body.suite_id,
                                     tier=body.tier, run_id=body.run_id)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="REPLAY_SET_CREATED",
                payload={"suite_id": body.suite_id, "run_id": body.run_id,
                         "tier": body.tier,
                         "case_count": replay.get("case_count")})
    db.commit()
    return {"item": replay}


@router.get("/replay-set/{suite_id}/{tier}")
def replay_set_cases(suite_id: str, tier: str,
                    user: User = Depends(require_min_role(ROLE_LEADER)),
                    db: Session = Depends(get_db)):
    """回放集 hydration：golden-follower / HTTP Target 执行契约的 case 列表。"""
    if tier not in ("smoke", "core", "full", "security", "voice"):
        raise HTTPException(status_code=400,
                            detail="tier 须为 smoke/core/full/security/voice")
    if db.get(EvalSuite, suite_id) is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {"suite_id": suite_id, "tier": tier,
            "cases": rg.replay_cases(db, suite_id=suite_id, tier=tier)}


# ---- V3-6 ：版本发布证据包（评测/安全/成本/性能/批准/回滚 一条链）----


@router.post("/evidence-export", status_code=201)
def evidence_export(body: EvidenceExportBody,
                    user: User = Depends(require_min_role(ROLE_LEADER)),
                    db: Session = Depends(get_db)):
    """导出发布证据包（只读证据快照，绝不改任何 Run/退款副作用）。"""
    try:
        item = rg.export_evidence_package(
            db, run_id=body.run_id, suite_id=body.suite_id,
            actor_id=str(user.id), code_ref=body.code_ref, note=body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVIDENCE_PACKAGE_EXPORTED",
                payload={"suite_id": body.suite_id, "run_id": body.run_id,
                         "package_version": item.get("package_version"),
                         "fp": item.get("fingerprint"),
                         "idempotent": item.get("idempotent")})
    db.commit()
    return {"item": item}


@router.get("/evidence-packages")
def evidence_packages_list(user: User = Depends(require_min_role(ROLE_LEADER)),
                          db: Session = Depends(get_db),
                          suite_id: str | None = None,
                          run_id: str | None = None,
                          limit: int = 20):
    """证据包列表（按套件/Run 过滤；只读）。"""
    return {"items": rg.list_evidence_packages(db, suite_id=suite_id,
                                               run_id=run_id, limit=min(limit, 100))}


@router.get("/evidence-packages/{package_id}")
def evidence_packages_get(package_id: str,
                          user: User = Depends(require_min_role(ROLE_LEADER)),
                          db: Session = Depends(get_db)):
    """证据包详情（完整关联引用 + 评测/安全/成本/性能/批准/回滚）。"""
    try:
        return {"item": rg.get_evidence_package(db, package_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="证据包不存在")