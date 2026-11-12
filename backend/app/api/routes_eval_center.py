# -*- coding: utf-8 -*-
"""评测中心对外接口（Evaluation Suite 数据集管理）。

`/api/v1/evaluation-center/datasets`：
  - 数据集（Suite）列表 / 详情 / 用例明细；
  - 从当前 Golden 快照创建不可变基线（幂等，同名同校验和不重复建）；
  - 复制建新版（基线只读），支持按用例局部编辑，改版走红线校验；
  - changelog / checksum 全维返回。

红线对齐：
  - 从不原地修改 Suite；修改只发生在复制出的新版本上；
  - 复制前对文档做资金副作用安全校验（SUCCESS 仅 APPROVED、非 APPROVED 禁 refund_execution、
    预留能力标签必须 skip_if_unsupported 且非终态）；
  - supervisor+ 可读评测资产并发起一键只读评测；leader+ 可治理数据集/Prompt/调度；
    admin 才可删除（删除仅在无 Run 引用时放行）。
"""
import json
import uuid as _uuid

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

MAX_OFFICE_BYTES = 10 * 1024 * 1024

from app.api.deps import ROLE_ADMIN, ROLE_LEADER, ROLE_SUPERVISOR, require_min_role
from app.domain.models import (
    EVAL_RUN_COMPLETED,
    EVAL_RUN_PENDING,
    EVAL_RUN_RUNNING,
    EVAL_SUITE_ACTIVE,
    EvalCase,
    EvalRun,
    EvalSuite,
    PromptVersion,
    User,
)
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.infrastructure.redis_client import STREAM_EVAL_RUN, xadd
from app.services.eval_center import (
    cancel_run,
    category_distribution,
    compare_suite_versions,
    copy_suite,
    create_ab_run,
    create_run,
    create_suite_from_golden,
    get_run,
    get_suite,
    get_suite_cases,
    list_ab_bundles,
    list_prompt_versions,
    list_run_results,
    list_runs,
    list_suites,
    run_progress,
    transition_prompt_status,
    trigger_regression,
)
from app.services.report_service import compare_a_b, compare_runs, list_reports, read_report
from app.services.evaluation_scoring import build_evaluation_scorecard

router = APIRouter(prefix="/evaluation-center", tags=["evaluation-center"])


class CreateSuiteBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    changelog: str = Field(default="", max_length=500)


class CopySuiteBody(BaseModel):
    new_name: str = Field(min_length=1, max_length=80)
    changelog: str = Field(default="", max_length=500)
    edits: list[dict] = Field(default_factory=list,
                              description="[{case_id, fields}] 覆盖到新版本；空=纯复制冻结")


@router.get("/datasets")
def datasets(page: int = 1, size: int = 20, category: str | None = None,
             user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
             db: Session = Depends(get_db)):
    """数据集列表（版本最新优先；category 按核心/边界/异常/并发/安全/语音筛选）。"""
    page = max(1, page)
    size = max(1, min(size, 100))
    return list_suites(db, page=page, size=size, category=category)


@router.post("/datasets", status_code=201)
def create_dataset(body: CreateSuiteBody,
                   user: User = Depends(require_min_role(ROLE_LEADER)),
                   db: Session = Depends(get_db)):
    """从当前 Golden 快照创建不可变数据集版本（幂等）。"""
    item = create_suite_from_golden(db, name=body.name, changelog=body.changelog,
                                    owner_id=str(user.id))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="DATASET_CREATED",
                payload={"suite_id": item["id"], "name": body.name,
                         "changelog": body.changelog})
    db.commit()
    return {"item": item}


# 注意：/datasets/categories、/datasets/compare 为静态路径，必须在
# /datasets/{suite_id} 之前注册，否则会被 {suite_id} 误吞（同 /reports/compare 教训）。
@router.get("/datasets/categories")
def dataset_categories(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                       db: Session = Depends(get_db)):
    """数据集类别分布（核心/边界/异常/并发/安全/语音 + 总数）。"""
    dist = category_distribution(db)
    return {"total": dist["total"], "categories": dist["categories"]}


@router.get("/datasets/compare")
def dataset_compare(base_suite_id: str, target_suite_id: str,
                    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                    db: Session = Depends(get_db)):
    """数据集版本对比：逐 case added/removed/changed + 类别分布变化。"""
    try:
        return compare_suite_versions(db, base_suite_id, target_suite_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"数据集不存在: {exc}")


@router.get("/datasets/{suite_id}")
def dataset_detail(suite_id: str,
                   user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                   db: Session = Depends(get_db)):
    item = get_suite(db, suite_id)
    if item is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {"item": item}


@router.get("/datasets/{suite_id}/cases")
def dataset_cases(suite_id: str,
                  user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                  db: Session = Depends(get_db)):
    """数据集用例明细（供评测中心预览/导出）。"""
    if get_suite(db, suite_id) is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {"suite_id": suite_id, "cases": get_suite_cases(db, suite_id)}


@router.post("/datasets/{suite_id}/copy", status_code=201)
def copy_dataset(suite_id: str, body: CopySuiteBody,
                 user: User = Depends(require_min_role(ROLE_LEADER)),
                 db: Session = Depends(get_db)):
    """复制建新版（基线只读）。edits 可按 case 局部覆盖；含红线校验。"""
    try:
        item = copy_suite(db, suite_id, new_name=body.new_name,
                          changelog=body.changelog, edits=body.edits,
                          owner_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="源数据集不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="DATASET_COPIED",
                payload={"source_suite_id": suite_id, "new_suite_id": item["id"],
                         "new_name": body.new_name})
    db.commit()
    return {"item": item}


@router.delete("/datasets/{suite_id}", status_code=204)
def delete_dataset(suite_id: str,
                   user: User = Depends(require_min_role(ROLE_ADMIN)),
                   db: Session = Depends(get_db)):
    """删除数据集（admin）。被 Run 引用时拒绝，保护评测证据链。"""
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    referenced = db.query(EvalRun).filter_by(suite_id=suite_id).count()
    if referenced:
        raise HTTPException(status_code=409,
                            detail=f"数据集仍被 {referenced} 个 Run 引用，不可删除")
    # 先删子表（EvalCase），避免 FK 违反；Run 已确认无引用
    db.query(EvalCase).filter_by(suite_id=suite_id).delete()
    db.delete(suite)
    db.commit()
    write_event(db, actor_id=str(user.id), case_id=None,
                action="DATASET_DELETED",
                payload={"suite_id": suite_id, "name": suite.name})
    db.commit()
    return None


class CreateRunBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    suite_id: str = Field(min_length=1, max_length=36)
    limit: int = Field(default=0, ge=0, le=300)
    mode: str = Field(default="in_process", pattern="^(in_process|http)$")
    priority: int = Field(default=0, ge=0, le=100)
    target_type: str = Field(default="refund_workflow")
    prompt_id: str | None = Field(default=None,
                                  description="A-B prompt（APPROVED）或普通模式沿用")
    # ：并行 worker 数（0/1=串行，>1 并行执行并报告加速比/资源占用）
    concurrency: int = Field(default=0, ge=0, le=8,
                             description="并行 worker 数（0=串行；>1 并行并报告 A08 度量）")


@router.post("/runs", status_code=202)
def create_run_endpoint(body: CreateRunBody,
                        user: User = Depends(require_min_role(ROLE_LEADER)),
                        db: Session = Depends(get_db)):
    """创建异步评测 Run 并入队（PENDING），由独立 Evaluation Worker 执行。"""
    suite = db.get(EvalSuite, body.suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    run_id = f"run-{_uuid.uuid4().hex[:12]}"
    prompt_variant_id = None
    prompt_baseline_id = None
    if body.prompt_id:
        pv = db.get(PromptVersion, body.prompt_id)
        if pv is None:
            raise HTTPException(status_code=404, detail="Prompt 版本不存在")
        if pv.status != "APPROVED":
            raise HTTPException(status_code=400,
                                detail=f"Prompt 尚未人工批准（{pv.status}），不可参与评测")
        if pv.is_baseline:
            prompt_baseline_id = pv.id
        else:
            prompt_variant_id = pv.id
    run = create_run(db, run_id=run_id, name=body.name, suite=suite,
                     created_by=str(user.id), target_type=body.target_type,
                     config={"mode": body.mode, "limit": body.limit,
                             "concurrency": body.concurrency},
                     prompt_variant_id=prompt_variant_id,
                     prompt_baseline_id=prompt_baseline_id)
    xadd(STREAM_EVAL_RUN, {
        "case_id": run_id, "run_id": run_id, "suite_id": body.suite_id,
        "limit": str(body.limit), "priority": str(body.priority),
        "target_type": body.target_type,
        "config": json.dumps({"mode": body.mode, "limit": body.limit,
                              "concurrency": body.concurrency}),
    })
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVAL_RUN_CREATED",
                payload={"run_id": run_id, "suite_id": body.suite_id,
                         "name": body.name, "mode": body.mode, "limit": body.limit,
                         "concurrency": body.concurrency})
    db.commit()
    return {"run_id": run_id, "status": run.status, "message": "已入队，由 Evaluation Worker 执行"}


@router.get("/runs")
def list_run_endpoints(page: int = 1, size: int = 20,
                       user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                       db: Session = Depends(get_db)):
    return list_runs(db, page=max(1, page), size=max(1, min(size, 100)))


@router.post("/runs/one-click", status_code=202)
def create_one_click_run(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """选取最新活动数据集并发起全量安全回归，无需用户填写表单。

    进程内 GoldenFollower 只做评测，不连接支付渠道，也不修改业务订单。
    连续点击时复用仍在排队/运行的同类任务，避免重复消耗资源。
    """
    active_runs = (
        db.query(EvalRun)
        .filter(EvalRun.status.in_([EVAL_RUN_PENDING, EVAL_RUN_RUNNING]))
        .order_by(EvalRun.created_at.desc())
        .all()
    )
    for existing in active_runs:
        if bool((existing.config or {}).get("one_click")):
            # PENDING 可能来自 API 已落库、但进程在 Redis 投递前异常退出的窗口。
            # 重复点击时安全重投同一个 run_id；Worker 的状态检查与 case 锁会去重，
            # 从而既不创建第二个 Run，也不会让任务永久卡在“排队中”。
            if existing.status == EVAL_RUN_PENDING:
                retry_config = existing.config or {
                    "mode": "in_process", "limit": 0,
                    "concurrency": 4, "one_click": True,
                }
                xadd(STREAM_EVAL_RUN, {
                    "case_id": existing.run_id,
                    "run_id": existing.run_id,
                    "suite_id": existing.suite_id or "",
                    "limit": str(retry_config.get("limit") or 0),
                    "priority": str(existing.priority or 0),
                    "target_type": existing.target_type or "refund_workflow",
                    "config": json.dumps(retry_config),
                })
            return {
                "run_id": existing.run_id,
                "status": existing.status,
                "deduplicated": True,
                "message": "已有一键评测正在执行，已为你打开该任务",
            }

    suite = (
        db.query(EvalSuite)
        .filter_by(status=EVAL_SUITE_ACTIVE)
        .order_by(EvalSuite.created_at.desc())
        .first()
    )
    if suite is None:
        item = create_suite_from_golden(
            db,
            name="一键评测自动基线",
            changelog="由评测中心首次一键评测自动创建",
            owner_id=str(user.id),
        )
        suite = db.get(EvalSuite, item["id"])

    run_id = f"run-{_uuid.uuid4().hex[:12]}"
    config = {"mode": "in_process", "limit": 0, "concurrency": 4, "one_click": True}
    run = create_run(
        db,
        run_id=run_id,
        name="一键全量安全回归",
        suite=suite,
        created_by=str(user.id),
        target_type="refund_workflow",
        config=config,
        trigger_source="manual",
    )
    xadd(STREAM_EVAL_RUN, {
        "case_id": run_id,
        "run_id": run_id,
        "suite_id": suite.id,
        "limit": "0",
        "priority": "0",
        "target_type": "refund_workflow",
        "config": json.dumps(config),
    })
    write_event(
        db,
        actor_id=str(user.id),
        case_id=None,
        action="EVAL_ONE_CLICK_STARTED",
        payload={"run_id": run_id, "suite_id": suite.id, "read_only": True},
    )
    db.commit()
    return {
        "run_id": run_id,
        "status": run.status,
        "suite_id": suite.id,
        "suite_version": suite.version,
        "deduplicated": False,
        "message": "全量评测已入队，评测不会执行真实退款",
    }


@router.get("/scorecard/latest")
def latest_evaluation_scorecard(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """返回最近一次已完成 Run 的三维实测评分；无报告时明确 PENDING。"""
    run = (
        db.query(EvalRun)
        .filter_by(status=EVAL_RUN_COMPLETED, target_type="refund_workflow")
        .order_by(EvalRun.created_at.desc())
        .first()
    )
    if run is None:
        return build_evaluation_scorecard(None)
    try:
        report = read_report(db, run.run_id)
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        # Run 已完成但报告暂不可读取，不能编造分数。
        return build_evaluation_scorecard(None)
    return build_evaluation_scorecard(report)


# 注意：/runs/ab 的 GET/POST 必须在 GET /runs/{run_id} 之前注册，
# 否则会被 {run_id}=ab 吞掉 -> 404（同 /reports/compare 教训）。
class CreateRunABBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    suite_id: str = Field(min_length=1, max_length=36)
    baseline_id: str = Field(min_length=1, max_length=36)
    variant_id: str = Field(min_length=1, max_length=36)
    limit: int = Field(default=0, ge=0, le=300)
    mode: str = Field(default="in_process", pattern="^(in_process|http)$")
    # ：A-B 对也可并行执行（保序 + 加速比/资源报告）
    concurrency: int = Field(default=0, ge=0, le=8)


@router.post("/runs/ab", status_code=202)
def create_ab_run_endpoint(body: CreateRunABBody,
                           user: User = Depends(require_min_role(ROLE_LEADER)),
                           db: Session = Depends(get_db)):
    """在相同数据集上创建 baseline/candidate 成对 Run（A-B 前缀）。

    绑定的 Prompt 必须已人工批准；A-B 结果由 /reports/ab/{bundle_id} 聚合。
    """
    suite = db.get(EvalSuite, body.suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    try:
        bundle = create_ab_run(db, name=body.name, suite=suite,
                               baseline_id=body.baseline_id,
                               variant_id=body.variant_id,
                               created_by=str(user.id), limit=body.limit,
                               mode=body.mode, concurrency=body.concurrency)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Prompt 版本不存在: {exc}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVAL_AB_CREATED",
                payload={"bundle_id": bundle.get("bundle_id"),
                         "baseline_run_id": bundle.get("baseline_run", {}).get("run_id"),
                         "variant_run_id": bundle.get("variant_run", {}).get("run_id"),
                         "suite_id": body.suite_id})
    db.commit()
    return bundle


@router.get("/runs/ab")
def list_ab(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
            db: Session = Depends(get_db)):
    return list_ab_bundles(db)


@router.get("/runs/{run_id}")
def run_detail(run_id: str,
               user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
               db: Session = Depends(get_db)):
    item = get_run(db, run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    return {"item": item}


@router.get("/runs/{run_id}/results")
def run_results(run_id: str,
                user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                db: Session = Depends(get_db)):
    item = get_run(db, run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    return {"run_id": run_id, "status": item["status"],
            "results": list_run_results(db, run_id)}


@router.get("/runs/{run_id}/progress")
def run_progress_endpoint(run_id: str,
                          user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                          db: Session = Depends(get_db)):
    """运行进度（done/total + 来源）。页面轮询用。"""
    try:
        return run_progress(db, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Run 不存在")


@router.post("/runs/{run_id}/cancel")
def run_cancel(run_id: str,
               user: User = Depends(require_min_role(ROLE_LEADER)),
               db: Session = Depends(get_db)):
    try:
        item = cancel_run(db, run_id, actor_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Run 不存在")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVAL_RUN_CANCELLED",
                payload={"run_id": run_id, "status": item.get("status")})
    db.commit()
    return {"item": item}


# ---- V2.0 -2 触发回归（代码/Prompt/模型/策略变更 → 自动回归）----

class CreateRegressionBody(BaseModel):
    trigger_source: str = Field(pattern="^(code|prompt|model|policy)$")
    suite_id: str | None = Field(default=None)
    reason: str = Field(default="", max_length=500)


@router.post("/regressions/trigger", status_code=202)
def regressions_trigger(body: CreateRegressionBody,
                        user: User = Depends(require_min_role(ROLE_LEADER)),
                        db: Session = Depends(get_db)):
    """变更即触发回归（来源可追溯落 run.trigger_source）；同一来源防抖不重复排队。"""
    try:
        item = trigger_regression(db, trigger_source=body.trigger_source,
                                  reason=body.reason, suite_id=body.suite_id,
                                  created_by=str(user.id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"数据集不存在: {exc}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVAL_REGRESSION_TRIGGERED",
                payload={"run_id": item.get("run_id"),
                         "trigger_source": item.get("trigger_source"),
                         "suite_id": item.get("suite_id"),
                         "dedup": item.get("dedup", False)})
    db.commit()
    return item


@router.get("/reports")
def reports_list(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                 db: Session = Depends(get_db)):
    """报告列表：历史 V1 归档 + DB 中未归档的 Run。"""
    return {"reports": list_reports(db)}


@router.get("/reports/compare")
def report_compare(run_a: str, run_b: str,
                   user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                   db: Session = Depends(get_db)):
    """Run↔Run 对比：质量/状态/P95/成本 + 共同 case 回归/改善 diff。

    注意：须在 /reports/{run_id} 之前注册，否则 compare 会被 {run_id} 吞掉。
    """
    try:
        return compare_runs(db, run_a, run_b)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"报告不存在: {exc}")


@router.get("/reports/{run_id}")
def report_detail(run_id: str,
                  user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                  db: Session = Depends(get_db)):
    """读取一份报告（优先 reports/*.json 归档，其次 DB 重建）。"""
    try:
        doc = read_report(db, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"item": doc}


# ---- M14 Prompt/模型 A-B ----

class CreatePromptBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    agent_name: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1)
    model_name: str | None = Field(default=None, max_length=100)
    changelog: str = Field(default="", max_length=500)
    is_baseline: bool = Field(default=False)


class PromptTransitionBody(BaseModel):
    to_status: str = Field(pattern="^(CANDIDATE|APPROVED|REJECTED)$")


@router.get("/prompts")
def prompts_list(agent_name: str | None = None,
                 user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                 db: Session = Depends(get_db)):
    """Prompt 版本列表（默认隐藏未批准正文）。"""
    return list_prompt_versions(db, agent_name=agent_name or None)


@router.post("/prompts", status_code=201)
def prompts_create(body: CreatePromptBody,
                   user: User = Depends(require_min_role(ROLE_LEADER)),
                   db: Session = Depends(get_db)):
    """保存候选 Prompt 版本（初始 DRAFT，绝不自动 APPROVED）。"""
    from app.services.eval_center import create_prompt_version
    item = create_prompt_version(
        db, name=body.name, agent_name=body.agent_name, content=body.content,
        model_name=body.model_name, changelog=body.changelog,
        creator_id=str(user.id), is_baseline=body.is_baseline)
    write_event(db, actor_id=str(user.id), case_id=None,
                action="PROMPT_CREATED",
                payload={"prompt_id": item["id"], "name": body.name,
                         "agent_name": body.agent_name, "status": item["status"]})
    db.commit()
    return {"item": item}


@router.post("/prompts/{prompt_id}/transition")
def prompt_transition(prompt_id: str, body: PromptTransitionBody,
                      user: User = Depends(require_min_role(ROLE_LEADER)),
                      db: Session = Depends(get_db)):
    """人工状态迁移（DRAFT→CANDIDATE→APPROVED；唯一放行方向人工）。"""
    try:
        item = transition_prompt_status(db, prompt_id, target=body.to_status,
                                        approver=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Prompt 版本不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action=f"PROMPT_{item.get('status')}",
                payload={"prompt_id": prompt_id, "to_status": body.to_status})
    db.commit()
    return {"item": item}


@router.get("/reports/ab/{bundle_id}")
def ab_report(bundle_id: str,
              user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
              db: Session = Depends(get_db)):
    """A-B 结果：聚合同一 bundle 的 baseline/candidate 质量 + token Δ。"""
    rows = db.query(EvalRun).filter(EvalRun.config.isnot(None)).all()
    pair: dict[str, str] = {}
    for r in rows:
        cid = (r.config or {}).get("ab_bundle")
        if cid != bundle_id:
            continue
        if r.prompt_baseline_id:
            pair["baseline"] = r.run_id
        if r.prompt_variant_id:
            pair["variant"] = r.run_id
    if not pair:
        raise HTTPException(status_code=404, detail="A-B bundle 不存在")
    try:
        return compare_a_b(db, pair["baseline"], pair["variant"])
    except KeyError:
        raise HTTPException(status_code=404, detail="A-B 报告不存在（Run 尚未完成）")


# ---- M21 插件契约：能力声明（按真实插件注册状态返回）----

@router.get("/capabilities")
def capabilities_get(user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                     db: Session = Depends(get_db)):
    """能力注册表（M21插件契约）。

    security/intent/voice/coaching 已由 capability_plugin 基于真实实现注册为
    available；若插件被关闭或加载失败，则保守回退 unavailable，绝不伪造通过。
    """
    try:
        from eval_refund.capabilities import (
            capability_table,
            metric_table,
            unimplemented_metrics,
        )
    except Exception:  # noqa: BLE001 —— 契约模块缺失 -> 全员不可用（保守）
        from app.worker.eval_processor import _capabilities_doc
        doc = _capabilities_doc()
        return {"implemented": doc["core"],
                "capabilities": doc["declared"],
                "unimplemented": list(doc["declared"]),
                "unimplemented_metrics": [], "metrics": {}}
    tbl = capability_table()
    return {
        "implemented": [k for k, v in tbl.items() if v.get("implemented")],
        "capabilities": tbl,
        "unimplemented": [k for k, v in tbl.items()
                          if v.get("availability") != "available"],
        "unimplemented_metrics": unimplemented_metrics(),
        "metrics": metric_table(),
    }


# ---- M15 Office 沙箱导入/导出 ----

class ConfirmImportBody(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    changelog: str = Field(default="", max_length=500)


@router.post("/office/import-preview")
def office_import_preview(file: UploadFile = File(...),
                          user: User = Depends(require_min_role(ROLE_LEADER)),
                          db: Session = Depends(get_db)):
    """上传 CSV/XLSX 在沙箱内解析 -> 预览（不落库）。

    红线：FileGate 前置校验（扩展名/大小/路径/伪造扩展名）；解析只读结构，
    内容任何指令都只是数据，绝不执行；返回 case 数 + 前 5 条预览。
    """
    from app.services.office_sandbox import (
        FileGateError, imported_rows_to_doc, load_import_preview,
        parse_csv, parse_xlsx, store_import_preview, validate_upload,
    )
    data = file.file.read(MAX_OFFICE_BYTES + 1)
    try:
        safe = validate_upload(file.filename, data)
    except FileGateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        if safe.endswith(".csv"):
            rows = parse_csv(data)
        elif safe.endswith(".xlsx"):
            rows = parse_xlsx(data)
        else:
            raise HTTPException(status_code=400, detail="仅支持 csv/xlsx 导入")
    except FileGateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    doc = imported_rows_to_doc(rows)
    # 红线预检（SUCCESS 仅 APPROVED 等），通过才给预览
    from app.services.eval_center import _validate_doc
    try:
        _validate_doc(doc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"导入数据红线校验失败: {e}")
    token = store_import_preview(doc)
    rows_out = doc.get("cases", [])[:5]
    return {
        "token": token,
        "file_name": file.filename,
        "case_count": len(doc.get("cases") or []),
        "preview": [{
            "id": c.get("id"), "case_type": c.get("case_type"),
            "final_decision": (c.get("expected") or {}).get("final_decision"),
            "refund_status": (c.get("expected") or {}).get("refund_status"),
            "reason": (c.get("reason") or "")[:40],
        } for c in rows_out],
        "message": "预览成功，确认后落库为数据集版本",
    }


@router.post("/office/import-confirm", status_code=201)
def office_import_confirm(body: ConfirmImportBody,
                          user: User = Depends(require_min_role(ROLE_LEADER)),
                          db: Session = Depends(get_db)):
    """人工确认预览导入数据 -> 创建数据集版本（只读基线），并回收预览文件。"""
    from app.services.eval_center import create_suite_from_golden
    from app.services.office_sandbox import (
        consume_import_preview, load_import_preview,
    )
    doc = load_import_preview(body.token)
    if doc is None:
        raise HTTPException(status_code=400, detail="预览不存在或已过期")
    try:
        item = create_suite_from_golden(
            db, name=body.name, changelog=body.changelog,
            owner_id=str(user.id), import_doc=doc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    consume_import_preview(body.token)
    write_event(db, actor_id=str(user.id), case_id=None,
                action="OFFICE_IMPORTED",
                payload={"suite_id": item["id"], "name": body.name,
                         "case_count": len(doc.get("cases") or [])})
    db.commit()
    return {"item": item, "message": "已创建数据集版本（导入预览文件已回收）"}


@router.get("/reports/{run_id}/export")
def report_export(run_id: str, fmt: str = "xlsx",
                  user: User = Depends(require_min_role(ROLE_LEADER)),
                  db: Session = Depends(get_db)):
    """Export report to xlsx/csv/docx (sandbox-generated bytes, no file left on disk)."""
    from app.services.office_sandbox import export_report
    try:
        doc = read_report(db, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="报告不存在")
    fmt = fmt.lower()
    mime = {
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "csv": "text/csv",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if fmt not in mime:
        raise HTTPException(status_code=400, detail="仅支持 xlsx/csv/docx")
    data = export_report(doc, fmt)
    write_event(db, actor_id=str(user.id), case_id=None,
                action="REPORT_EXPORTED",
                payload={"run_id": run_id, "fmt": fmt})
    db.commit()
    return Response(
        content=data,
        media_type=mime[fmt],
        headers={"Content-Disposition":
                 f'attachment; filename="{run_id}.{fmt}"'})
