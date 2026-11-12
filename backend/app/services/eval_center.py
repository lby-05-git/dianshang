# -*- coding: utf-8 -*-
"""评测中心服务（Evaluation Suite / 版本化 + 管理）。

职责：
- 数据集（Suite）列表 / 版本化：从 eval_refund.golden 快照当前不可变基线；
  修改通过复制创建新版本（基线只读）；
- 单 Case 快照落库（EvalCase），后续 Runner 从 Suite 加载而非全局常量；
- Run 结果持久化辅助：EvalRun + EvalCaseResult（M12/M13 复用）；
- 评测数据与业务隔离：Suite/Run/Result 与 RefundCase 互不污染；
  Run 关联 dataset_checksum，校验和变化拒绝创建（不可变语义）。

红线：从不原地修改 Suite；从不伪造 measured 指标。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.domain.models import (
    EVAL_RESULT_SKIPPED_UNSUPPORTED,
    EVAL_RUN_CANCELLED,
    EVAL_RUN_COMPLETED,
    EVAL_RUN_FAILED,
    EVAL_RUN_PENDING,
    EVAL_RUN_RUNNING,
    EVAL_SUITE_ACTIVE,
    EVAL_TIERS,
    PROMPT_APPROVED,
    PROMPT_CANDIDATE,
    PROMPT_DRAFT,
    PROMPT_REJECTED,
    DatasetCategoryMap,
    EvalAlert,
    EvalCase,
    EvalCaseResult,
    EvalRun,
    EvalSchedule,
    EvalSuite,
    PromptVersion,
    new_uuid,
)

from datetime import timedelta

# ---- 数据集快照（来自 eval_refund.golden，版本不可变）----

_SCHEMA_VERSION = "1.0"

# ---- V2.0 -2 数据集分类（core/boundary/anomaly/concurrency/security/voice）----
# 与 scripts/refresh_dataset.py 镜像（保持行为一致；refresh_dataset 对冻结 golden 落库存量，
# 此处提供给任意 Suite 幂等复算）。add-only 红线：分类只写独立表，绝不改 case 内容。
V2_DATASET_CATEGORIES = (
    "core", "boundary", "anomaly", "concurrency", "security", "voice",
)

# ---- 变更触发来源（eval_runs.trigger_source）：代码/Prompt/模型/策略/人工/定时 ----
TRIGGER_SOURCES = ("code", "prompt", "model", "policy", "manual", "schedule")
CHANGE_TRIGGER_SOURCES = ("code", "prompt", "model", "policy")


def classify_case(case: dict) -> tuple[str, str]:
    """返回 (category, reason)。优先 case_type，次优 tags（与 refresh_dataset 同语义）。

    覆盖全部 case_type；保底 core（default），保证可复算不抛错。
    """
    _CASE_TYPE_CATEGORY = {
        "auto_approve": ("core", "主链路自动审批"),
        "high_value_approve": ("core", "高金额审批"),
        "high_value_reject": ("core", "高金额拒绝"),
        "verification_all_match": ("core", "核验全匹配"),
        "refund_status_success": ("core", "退款成功回执"),
        "channel_cs_agent": ("core", "客服代提"),
        "channel_mini_program": ("boundary", "小程序渠道"),
        "refund_status": ("core", "三态一致"),
        "refund_success": ("core", "退款成功"),
        "human_approve_release": ("core", "人工审批放行"),
        "high_value_approve_leader": ("core", "leader 级审批"),
        "high_value_reject_leader": ("core", "leader 级拒绝"),
        "threshold_carding": ("boundary", "阈值卡控"),
        "threshold_carding_boundary": ("boundary", "阈值临界"),
        "partial_refund": ("boundary", "部分退款"),
        "idempotency": ("concurrency", "幂等重放"),
        "idempotent_recreate": ("concurrency", "幂等重建"),
        "refund_retry": ("anomaly", "退款重试"),
        "product_mismatch": ("anomaly", "商品不一致"),
        "product_mismatch_sku": ("anomaly", "SKU 不一致"),
        "price_mismatch": ("anomaly", "价格不一致"),
        "price_mismatch_partial_refunded": ("anomaly", "价格不一致-部分退款"),
        "order_not_found": ("anomaly", "订单不存在"),
        "owner_mismatch": ("anomaly", "归属不符"),
        "ocr_low_confidence": ("anomaly", "OCR 低置信"),
        "ocr_failure": ("anomaly", "OCR 失败"),
        "fraud_evidence_reuse": ("anomaly", "凭证复用欺诈"),
        "provider_timeout": ("anomaly", "Provider 超时"),
        "provider_unavailable": ("anomaly", "Provider 不可用"),
        "llm_unavailable": ("anomaly", "LLM 不可用"),
        "system_failure_no_refund": ("anomaly", "系统故障不退款"),
        "reserved_security": ("security", "安全能力预留"),
        "reserved_intent": ("security", "意图预留（安全回落）"),
        "reserved_voice": ("voice", "语音预留"),
        "reserved_coaching": ("voice", "陪练预留"),
        "skipped_unsupported": ("security", "不支持回落安全"),
    }
    _TAG_TO_CATEGORY = {
        "security": "security", "intent": "security",
        "voice": "voice", "coaching": "voice",
    }
    ct = case.get("case_type") or ""
    if ct in _CASE_TYPE_CATEGORY:
        return _CASE_TYPE_CATEGORY[ct]
    for t in case.get("tags") or []:
        if t in _TAG_TO_CATEGORY:
            return _TAG_TO_CATEGORY[t], f"tag:{t}"
    for kw, cat in (
        ("idempot", "concurrency"), ("retry", "anomaly"),
        ("mismatch", "anomaly"), ("not_found", "anomaly"),
        ("timeout", "anomaly"), ("unavailable", "anomaly"),
        ("failure", "anomaly"),
    ):
        if kw in ct:
            return cat, f"case_type:{ct}"
    return "core", "default"


def _map_verdict(v: str | None) -> str:
    """判定符 -> 存储常量；skipped 映射到 SKIPPED_UNSUPPORTED，其余如实抬头。"""
    raw = (v or "ERROR").upper()
    if raw in ("SKIPPED", "SKIPPED_UNSUPPORTED"):
        return EVAL_RESULT_SKIPPED_UNSUPPORTED
    return raw


def _canonical(data: Any) -> bytes:
    import json
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _checksum(data: dict) -> str:
    return hashlib.sha256(_canonical(data)).hexdigest()


def current_golden_document() -> dict:
    """加载当前 GOLDEN 数据集文档（快照源）。"""
    from eval_refund.golden import build_dataset_document  # 软依赖，避免 backend 启动时强依赖根目录包
    return build_dataset_document()


def create_suite_from_golden(db: Session, *, name: str, changelog: str = "",
                             owner_id: str | None = None,
                             import_doc: dict | None = None) -> dict:
    """从当前 Golden 快照创建不可变 Suite，同时落 EvalCase 快照。

    M11：数据集版本化入口（V1.0 15 条 -> V2.0 30 条）。import_doc 供 Office
    沙箱导入（M15）走同一条落库通道；None 时用 golden 当前文档。
    """
    doc = dict(import_doc or current_golden_document())
    checksum = str(doc.get("checksum") or _checksum(doc))
    # 防重复：同名同校验和不再建（幂等创建）
    exists = db.query(EvalSuite).filter_by(name=name, checksum=checksum).first()
    if exists:
        item = _suite_as_dict(exists)
        item["case_count"] = get_suite_case_count(db, exists.id)
        return item
    _validate_doc(doc)
    return _upsert_from_doc(db, name=name, changelog=changelog, doc=doc,
                            owner_id=owner_id, version_bump=False)


def _validate_doc(doc: dict) -> None:
    """数据集红线校验（改版靠复制 + 资金副作用安全）。

    与 test_golden 不变量对齐，杜绝"复制出新版时伪造通过"：
    - 预留能力标签（voice/intent/...）必须显式 skip_if_unsupported 且预期非终态；
    - SUCCESS 退款仅允许 final_decision=APPROVED；
    - 非 APPROVED 决策必须禁止 refund_execution。
    """
    reserved = set(doc.get("reserved_tags") or [])
    for c in doc.get("cases") or []:
        cid = c.get("id", "?")
        if set(c.get("tags") or []) & reserved:
            if not c.get("skip_if_unsupported"):
                raise ValueError(f"{cid}: 预留标签必须显式 skip_if_unsupported")
            if (c.get("expected") or {}).get("current_status") == "COMPLETED":
                raise ValueError(f"{cid}: 预留标签用例预期不得为终态")
        e = c.get("expected") or {}
        if e.get("refund_status") == "SUCCESS" and e.get("final_decision") != "APPROVED":
            raise ValueError(f"{cid}: 仅 APPROVED 允许 SUCCESS 退款")
        if e.get("final_decision") != "APPROVED" and \
                "refund_execution" not in (c.get("forbidden_nodes") or []):
            raise ValueError(f"{cid}: 非 APPROVED 决策必须禁止 refund_execution")


def copy_suite(db: Session, src_id: str, *, new_name: str, changelog: str,
               edits: list[dict] | None = None, owner_id: str | None = None) -> dict:
    """复制建新版（基线只读）。edits:[{case_id, fields: {...}}] 覆盖到新版本。

    用例编辑用浅拷贝合入（list/dict 就地复制），生成的文档重新做红线校验 +
    计算新 checksum 落库；原始 Suite 的 EvalCase 绝不改动。
    """
    src = db.get(EvalSuite, src_id)
    if src is None:
        raise KeyError(src_id)
    src_cases = get_suite_cases(db, src_id)
    merges = {e["case_id"]: e.get("fields", {}) for e in (edits or [])}
    if not merges and src.checksum:
        # 无编辑 = 纯复制（等价历史冻结副本）
        existing = db.query(EvalSuite).filter_by(name=new_name, checksum=src.checksum).first()
        if existing:
            raise ValueError("同名同校验和版本已存在，无需重复复制")
    new_cases: list[dict] = []
    for c in src_cases:
        out: dict = dict(c)
        ov = merges.get(out["case_id"])
        if ov:
            for k, v in ov.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = {**out[k], **v}
                elif isinstance(v, list) and isinstance(out.get(k), list):
                    out[k] = out[k] + v
                else:
                    out[k] = v
        # EvalCase 快照细节搬移回 document 结构
        inputs = out.pop("inputs", {}) or {}
        expected = out.pop("expected") or {}
        constraints = out.pop("constraints", {}) or {}
        for k, v in constraints.items():
            out.setdefault(k, v)
        for k, v in inputs.items():
            out.setdefault(k, v)
        out["expected"] = expected
        new_cases.append(out)
    doc = {
        **dict(src.snapshot or {}),
        "dataset_version": src.version,
        "case_count": len(new_cases),
        "reserved_tags": (src.snapshot or {}).get("reserved_tags") or [],
        "cases": new_cases,
    }
    _validate_doc(doc)
    return _upsert_from_doc(db, name=new_name, changelog=changelog, doc=doc,
                            owner_id=owner_id, version_bump=True)


def _upsert_from_doc(db: Session, *, name: str, changelog: str, doc: dict,
                     owner_id: str | None, version_bump: bool = False) -> dict:
    """落库一个全新 Suite（含 EvalCase 快照 + checksum）。version_bump 时继承基线 major 版本。"""
    version = str(doc.get("dataset_version") or "2.0.0")
    if version_bump:
        major = version.split(".")[0]
        minor = int((version.split(".")[1] if len(version.split(".")) > 1 else "0") or "0") + 1
        version = f"{major}.{minor}.0"
    checksum = _checksum(doc)
    suite = EvalSuite(
        id=new_uuid(),
        name=name,
        version=version,
        schema_version=str(doc.get("schema_version") or _SCHEMA_VERSION),
        tags=doc.get("tags"),
        modalities=doc.get("modalities"),
        required_capabilities=doc.get("required_capabilities"),
        tier_map=doc.get("tiers") or {},
        checksum=checksum,
        snapshot={k: v for k, v in doc.items() if k != "cases"},
        status=EVAL_SUITE_ACTIVE,
        owner_id=owner_id,
        changelog=changelog,
    )
    db.add(suite)
    db.flush()
    for case in doc.get("cases") or []:
        db.add(_case_row(suite.id, case))
    db.commit()
    item = _suite_as_dict(suite)
    item["case_count"] = len(doc.get("cases") or [])
    return item


def _case_row(suite_id: str, case: dict) -> EvalCase:
    case_ref = case.get("id") or case.get("case_id") or ""
    ext = {"security": None, "intent": None, "voice": None, "coaching": None}
    # 能力验证套件（capabilities_suite）：把完整用例快照存进私有命名空间
    # _cap_doc，hydrated_cases 再合并回 dict——golden 用例无此字段，行为不变。
    if case.get("capability"):
        ext["_cap_doc"] = {**case}
    return EvalCase(
        id=new_uuid(),
        suite_id=suite_id,
        case_ref_id=case_ref,
        case_type=case.get("case_type"),
        tags=case.get("tags"),
        category=case.get("category"),   # V2.0 数据集分类（core/boundary/anomaly/concurrency/security/voice）
        inputs={
            "order_id": case.get("order_id"),
            "requested_amount": case.get("requested_amount"),
            "reason": case.get("reason"),
            "channel": case.get("channel"),
            "reason_codes": case.get("reason_codes"),
            "as_user": case.get("as_user"),
        },
        expected=case.get("expected") or {},
        constraints={
            "allow": case.get("allow"),
            "max_wait_seconds": case.get("max_wait_seconds"),
            "required_nodes": case.get("required_nodes"),
            "forbidden_nodes": case.get("forbidden_nodes"),
            "side_effect": case.get("side_effect"),
            "skip_if_unsupported": case.get("skip_if_unsupported"),
            "approval_required": case.get("approval_required"),
            "reject_required": case.get("reject_required"),
            "use_unique_order": case.get("use_unique_order"),
        },
        extensions=ext,
    )


def list_suites(db: Session, *, page: int = 1, size: int = 20,
                category: str | None = None) -> dict:
    """数据集列表（版本最新优先）。

    category 非空时按数据集用例类别过滤：dataset_category_map 命中该类的
    Suite 才返回（核心/边界/异常/并发/安全/语音，V2.0 -2）。
    """
    q = db.query(EvalSuite)
    total = db.query(EvalSuite).count()
    if category:
        from sqlalchemy import func  # 与 category_distribution 同风格，避免顶层 sqlalchemy 耦合
        cat_map = {  # 该类别下出现的 suite 全集（含 case_ref_id，供过滤后仍回填计数）
            s_id: cnt for s_id, cnt in
            (db.query(DatasetCategoryMap.suite_id,
                      func.count(DatasetCategoryMap.id))
               .filter_by(category=category).group_by(DatasetCategoryMap.suite_id).all())
        }
        if not cat_map:
            return {"total": total, "items": []}
        q = q.filter(EvalSuite.id.in_(list(cat_map)))
    rows = (q.order_by(EvalSuite.created_at.desc())
            .offset((page - 1) * size).limit(size).all())
    items = [_suite_as_dict(r) for r in rows]
    if category:
        # 类别过滤语义：只显示该类别覆盖的用例数，避免同集标注全量 case_count 迷惑
        for i in items:
            i["case_count"] = cat_map.get(i["id"], 0)
    else:
        _fill_case_counts(db, items)
    return {"total": total, "items": items}


def get_suite(db: Session, suite_id: str) -> dict | None:
    row = db.get(EvalSuite, suite_id)
    if row is None:
        return None
    item = _suite_as_dict(row)
    _fill_case_counts(db, [item])
    return item


def get_suite_cases(db: Session, suite_id: str) -> list[dict]:
    rows = (db.query(EvalCase)
            .filter_by(suite_id=suite_id)
            .order_by(EvalCase.case_ref_id)
            .all())
    return [{
        "case_id": r.case_ref_id,
        "case_type": r.case_type,
        "tags": r.tags,
        "category": r.category,   # V2.0 数据集分类
        "inputs": r.inputs,
        "expected": r.expected,
        "constraints": r.constraints,
        "extensions": r.extensions,
    } for r in rows]


def _suite_as_dict(s: EvalSuite) -> dict:
    return {
        "id": s.id, "name": s.name, "version": s.version,
        "schema_version": s.schema_version, "tags": s.tags,
        "modalities": s.modalities,
        "required_capabilities": s.required_capabilities,
        "tier_map": s.tier_map or {},          # M18：{tier: [case ids]}
        "checksum": s.checksum, "status": s.status,
        "owner_id": s.owner_id, "changelog": s.changelog,
        "case_count": 0,  # 调用方用 _fill_case_counts 批量填充
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _fill_case_counts(db: Session, items: list[dict]) -> None:
    """批量填充 case_count，避免 N+1。"""
    if not items:
        return
    from sqlalchemy import func
    c = db.query(EvalCase.suite_id, func.count(EvalCase.id)) \
           .filter(EvalCase.suite_id.in_([i["id"] for i in items])) \
           .group_by(EvalCase.suite_id).all()
    counts = {sid: n for sid, n in c}
    for i in items:
        i["case_count"] = counts.get(i["id"], 0)


def get_suite_case_count(db: Session, suite_id: str | None) -> int:
    if not suite_id:
        return 0
    return db.query(EvalCase).filter_by(suite_id=suite_id).count()


# ---- Run 快照生成（M12 Runner 复用）----

def hydrated_cases(db: Session, suite_id: str) -> list[dict]:
    """把 Suite 的 EvalCase 反解为 runner 需要的 golden dict 列表。"""
    out = []
    for row in (db.query(EvalCase).filter_by(suite_id=suite_id)
                .order_by(EvalCase.case_ref_id).all()):
        c = {
            "id": row.case_ref_id,
            "case_type": row.case_type or "",
            "tags": row.tags or [],
            **row.inputs,
            "expected": row.expected or {},
        }
        if row.constraints:
            c.update({k: v for k, v in row.constraints.items() if v is not None})
        # 能力验证套件：合并完整用例快照（capability/tool_gate/intent_text/voice_frames/
        # coaching_turns 等专项字段），golden 无 _cap_doc，行为不变
        cap_doc = (row.extensions or {}).get("_cap_doc")
        if cap_doc:
            c.update(cap_doc)
        out.append(c)
    return out


# ---- Run 持久化（M12/M13 复用）----

def create_run(db: Session, *, run_id: str, name: str, suite, created_by: str | None,
               config: dict | None = None, prompt_variant_id: str | None = None,
               prompt_baseline_id: str | None = None, target_type: str = "refund_workflow",
               task_type: str = "refund_eval",
               trigger_source: str = "manual") -> EvalRun:
    """创建 PENDING Run（唯一 run_id，幂等由唯一约束兜底）。

    trigger_source（V2.0 -2 触发回归来源）：code/prompt/model/policy/manual/schedule，
    供计时评测与变更回归可追溯（列表/报告/对比均带出）。
    """
    run = EvalRun(
        id=new_uuid(),
        run_id=run_id,
        name=name,
        task_type=task_type,
        schema_version=_SCHEMA_VERSION,
        priority=0,
        status=EVAL_RUN_PENDING,
        suite_id=suite.id if suite else None,
        suite_version=suite.version if suite else None,
        dataset_checksum=suite.checksum if suite else None,
        target_type=target_type,
        base_url=None,
        created_by=created_by,
        config=config or {},
        prompt_variant_id=prompt_variant_id,
        prompt_baseline_id=prompt_baseline_id,
        trigger_source=trigger_source,
        trace_id=str(uuid.uuid4()),
    )
    db.add(run)
    db.commit()
    return run


def save_case_result(db: Session, run_id: str, result: dict, candidate: dict | None,
                     llm_judge: dict | None) -> None:
    """按 run/case 幂等落一条 Case 结果（同 run 同 case 覆盖替代）。"""
    # 已有则覆盖（Runner 失败隔离；SKIPPED_UNSUPPORTED 也落库）
    judge = result.get("judge") or {}
    execution = result.get("execution") or {}
    existing = (db.query(EvalCaseResult)
                .filter_by(run_id=run_id, case_ref_id=result.get("case_id", ""))
                .first())
    payload = {
        "run_id": run_id,
        "case_ref_id": result.get("case_id", ""),
        "case_type": result.get("case_type", ""),
        "verdict": _map_verdict(judge.get("verdict")),
        "score": judge.get("score"),
        "dimensions": {"dimensions": judge.get("dimensions")},
        "evidence": {"evidence": judge.get("evidence")},
        "judge_version": judge.get("judge_version"),
        "degraded": bool(result.get("degraded")),
        "degraded_reason": judge.get("degraded_reason") or result.get("error"),
        "execution": {
            "mode": execution.get("mode"),
            "latency_ms": execution.get("latency_ms"),
            "polling_rounds": execution.get("polling_rounds"),
        },
        "candidate": candidate,
        "llm_judge": llm_judge,
        "trace_id": (candidate or {}).get("trace_id"),
    }
    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
    else:
        db.add(EvalCaseResult(id=new_uuid(), **payload))
    db.commit()


def finish_run(db: Session, run_id: str, *, status: str, summary: dict | None = None,
               performance: dict | None = None, cost: dict | None = None,
               error: str | None = None) -> None:
    """收尾 Run（COMPLETED/FAILED/CANCELLED）。"""
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        return
    run.status = status
    if summary is not None:
        run.summary = summary
    if performance is not None:
        run.performance = performance
    if cost is not None:
        run.cost = cost
    if error is not None:
        run.error = error
    run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


def mark_run_started(db: Session, run_id: str) -> None:
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None or run.status != EVAL_RUN_PENDING:
        return
    run.status = "RUNNING"
    run.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


# ---- Run 查询 / 取消（M12 API 复用）----

def get_run(db: Session, run_id: str) -> dict | None:
    row = db.query(EvalRun).filter_by(run_id=run_id).first()
    return _run_as_dict(row) if row else None


def list_runs(db: Session, *, page: int = 1, size: int = 20) -> dict:
    rows = (db.query(EvalRun)
            .order_by(EvalRun.created_at.desc())
            .offset((page - 1) * size).limit(size).all())
    total = db.query(EvalRun).count()
    return {"total": total, "items": [_run_as_dict(r) for r in rows]}


def cancel_run(db: Session, run_id: str, *, actor_id: str | None = None) -> dict:
    row = db.query(EvalRun).filter_by(run_id=run_id).first()
    if row is None:
        raise KeyError(run_id)
    if row.status not in ("PENDING", "RUNNING", "FAILED"):
        raise ValueError(f"Run 状态 {row.status} 不可取消")
    row.status = EVAL_RUN_CANCELLED
    row.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return _run_as_dict(row)


def list_run_results(db: Session, run_id: str) -> list[dict]:
    rows = (db.query(EvalCaseResult)
            .filter_by(run_id=run_id)
            .order_by(EvalCaseResult.case_ref_id).all())
    return [_result_as_dict(r) for r in rows]


def _run_as_dict(r: EvalRun) -> dict:
    return {
        "run_id": r.run_id, "name": r.name, "task_type": r.task_type,
        "priority": r.priority, "status": r.status,
        "suite_id": r.suite_id, "suite_version": r.suite_version,
        "dataset_checksum": r.dataset_checksum,
        "target_type": r.target_type, "created_by": r.created_by,
        "config": r.config, "prompt_variant_id": r.prompt_variant_id,
        "prompt_baseline_id": r.prompt_baseline_id,
        "summary": r.summary, "performance": r.performance, "cost": r.cost,
        "report_path": r.report_path, "error": r.error, "trace_id": r.trace_id,
        "trigger_source": r.trigger_source,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _result_as_dict(r: EvalCaseResult) -> dict:
    return {
        "run_id": r.run_id, "case_id": r.case_ref_id, "case_type": r.case_type,
        "verdict": r.verdict, "score": r.score,
        "dimensions": r.dimensions, "evidence": r.evidence,
        "judge_version": r.judge_version, "degraded": r.degraded,
        "degraded_reason": r.degraded_reason, "execution": r.execution,
        "candidate": r.candidate, "llm_judge": r.llm_judge, "trace_id": r.trace_id,
    }


# ---- Prompt 版本簿记（M14 A-B 复用）----

def create_prompt_version(db: Session, *, name: str, agent_name: str, content: str,
                          model_name: str | None, changelog: str = "",
                          creator_id: str | None = None, is_baseline: bool = False) -> dict:
    """保存候选 Prompt 版本（初始 DRAFT/CANDIDATE，绝不自动 APPROVED）。"""
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = (db.query(PromptVersion)
                .filter_by(agent_name=agent_name, content_checksum=checksum).first())
    if existing:
        return _prompt_as_dict(existing)
    major = len(db.query(PromptVersion).filter_by(agent_name=agent_name).all()) + 1
    pv = PromptVersion(
        id=new_uuid(),
        name=name,
        agent_name=agent_name,
        version=f"v{major}",
        content=content,
        content_checksum=checksum,
        model_name=model_name,
        status=PROMPT_DRAFT,
        creator_id=creator_id,
        changelog=changelog,
        is_baseline=is_baseline,
    )
    db.add(pv)
    db.commit()
    return _prompt_as_dict(pv)


def _prompt_as_dict(p: PromptVersion) -> dict:
    return {
        "id": p.id, "name": p.name, "agent_name": p.agent_name,
        "version": p.version, "content": p.content,
        "content_checksum": p.content_checksum, "model_name": p.model_name,
        "status": p.status, "creator_id": p.creator_id,
        "approver_id": p.approver_id,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "changelog": p.changelog, "is_baseline": p.is_baseline,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ---- M14 Prompt/模型 A-B：版本生命周期 + 同数据集成对 Run ----

def list_prompt_versions(db: Session, *, agent_name: str | None = None,
                         include_all: bool = False) -> dict:
    """Prompt 版本列表（按 agent 过滤；默认只显示可用的 / 内容摘要）。"""
    q = db.query(PromptVersion).order_by(PromptVersion.agent_name,
                                          PromptVersion.version.desc())
    if agent_name:
        q = q.filter_by(agent_name=agent_name)
    rows = q.all()
    items = []
    for p in rows:
        item = _prompt_as_dict(p)
        item["content"] = item["content"][:200] + ("…" if len(item["content"]) > 200 else "")
        if not include_all and p.status in (PROMPT_DRAFT, PROMPT_CANDIDATE):
            item["content"] = "(未批准，内容隐藏)"  # 人工批准前对团队隐藏正文
        items.append(item)
    return {"total": len(items), "items": items}


def transition_prompt_status(db: Session, prompt_id: str, *, target: str,
                             approver: str | None = None) -> dict:
    """人工状态迁移：CANDIDATE -> APPROVED（唯一放行方向）；DRAFT -> CANDIDATE。

    红线：候选只能由人工批准进入 APPROVED，任何状态机都不自动。
    APPROVED/REJECTED 不可再改（版本历史冻结）。
    """
    pv = db.get(PromptVersion, prompt_id)
    if pv is None:
        raise KeyError(prompt_id)
    cur = pv.status
    allowed = {
        PROMPT_DRAFT: {PROMPT_CANDIDATE, PROMPT_REJECTED},
        PROMPT_CANDIDATE: {PROMPT_APPROVED, PROMPT_REJECTED},
        PROMPT_APPROVED: set(),
        PROMPT_REJECTED: set(),
    }
    if target not in allowed[cur]:
        raise ValueError(f"Prompt {pv.version} 状态 {cur} 不可迁移到 {target}")
    pv.status = target
    if target == PROMPT_APPROVED:
        from datetime import datetime as _dt, timezone as _tz
        pv.approver_id = approver
        pv.approved_at = _dt.now(_tz.utc).replace(tzinfo=None)
    db.commit()
    return _prompt_as_dict(pv)


def _require_verified_prompt(db: Session, prompt_id: str) -> PromptVersion:
    pv = db.get(PromptVersion, prompt_id)
    if pv is None:
        raise KeyError(prompt_id)
    if pv.status != PROMPT_APPROVED:
        raise ValueError(f"Prompt {pv.version} 尚未人工批准（{pv.status}），不可参与 A-B")
    return pv


def create_ab_run(db: Session, *, name: str, suite, baseline_id: str,
                  variant_id: str, created_by: str | None = None,
                  limit: int = 0, mode: str = "in_process",
                  target_type: str = "refund_workflow",
                  concurrency: int = 0) -> dict:
    """为同一数据集创建 baseline/candidate 成对 Run（A-B 前缀）。

    两个 Run 的 prompt_baseline_id / prompt_variant_id 明确指向同一对：
    - run-…-a-baseline : 绑 baseline 版本
    - run-…-b-variant  : 绑 candidate 版本
    绑定的 PromptVersion 必须 APPROVED（人工批准门，_require_verified_prompt）。
    """
    base = _require_verified_prompt(db, baseline_id)
    cand = _require_verified_prompt(db, variant_id)
    if base.agent_name != cand.agent_name:
        raise ValueError("baseline 与 candidate 必须是同一 Agent 的版本进行 A-B")
    bundle = uuid.uuid4().hex[:8]
    config = {"mode": mode, "limit": limit, "ab_bundle": bundle,
              "baseline": base.version, "variant": cand.version,
              # A08：A-B 对比同样可并行执行，速度/资源度量对两 Run 一致
              "concurrency": concurrency}
    base_run = create_run(db, run_id=f"run-{bundle}-a-baseline", name=f"{name} [A 基线]",
                          suite=suite, created_by=created_by, config=config,
                          prompt_baseline_id=baseline_id,
                          target_type=target_type)
    cand_run = create_run(db, run_id=f"run-{bundle}-b-variant", name=f"{name} [B 候选]",
                          suite=suite, created_by=created_by, config=config,
                          prompt_variant_id=variant_id,
                          target_type=target_type)
    return {
        "bundle_id": bundle,
        "baseline_run": {"run_id": base_run.run_id, "prompt_id": baseline_id,
                         "label": base.version},
        "variant_run": {"run_id": cand_run.run_id, "prompt_id": variant_id,
                        "label": cand.version},
        "config": config,
    }


def list_ab_bundles(db: Session, *, page: int = 1, size: int = 20) -> dict:
    """按 ab_bundle 聚合 Run 对（A 基线 / B 候选），供前端 A-B 结果页。

    MySQL 8 JSON 无 -> 索引（免迁移），分批扫最近 Run 在 Python 聚合即可。
    """
    rows = (db.query(EvalRun).filter(EvalRun.config.isnot(None))
            .order_by(EvalRun.created_at.desc()).limit(size * 2 + page).all())
    groups: dict[str, list[dict]] = {}
    for r in rows:
        cid = (r.config or {}).get("ab_bundle")
        if not cid:
            continue
        groups.setdefault(cid, []).append(_run_as_dict(r))
    bundles = []
    for cid, runs in groups.items():
        by_role = {}
        for r in runs:
            if r["prompt_baseline_id"]:
                by_role["baseline"] = r
            if r["prompt_variant_id"]:
                by_role["variant"] = r
        bundles.append({"bundle_id": cid, **by_role,
                        "created_at": max((x["created_at"] or "" for x in runs))})
    return {"total": len(groups), "items": bundles}


# ---- M18 分级调度（V3.0 ）----
# 调度只负责到期触发 Run；绝不自动修改任何决策/退款。绑定非 APPROVED Prompt
# 直接拒绝（人工批准门不因自动化绕开）。unique(suite_id, tier) 幂等。

def _next_due(cadence: str, now: datetime) -> datetime:
    if cadence == "daily":
        return now + timedelta(days=1)
    if cadence == "weekly":
        return now + timedelta(weeks=1)
    if cadence == "change":
        return now + timedelta(days=7)      # 变更驱动：仅当变更时才重置为立即
    return now + timedelta(days=7)          # manual：定时哨兵用，不自动跑


def create_schedule(db: Session, *, suite_id: str, tier: str, cadence: str,
                    enabled: bool = True, owner_id: str | None = None,
                    initial_delay_minutes: int = 0) -> dict:
    """为数据集创建分级调度（smoke/core/full × manual/daily/weekly/change）。"""
    if tier not in EVAL_TIERS:
        raise ValueError(f"tier 必须为 {EVAL_TIERS}")
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise KeyError(suite_id)
    existing = (db.query(EvalSchedule)
                .filter_by(suite_id=suite_id, tier=tier).first())
    if existing:
        item = _schedule_as_dict(existing)
        item["suite_name"] = suite.name
        return item
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # initial_delay_minutes=0 表示"立即到期可触发"：把 next_due 设为过去 60s，
    # 避免微秒/时区截断导致"刚好 == now"被判未到期的不稳。
    if initial_delay_minutes:
        due = now + timedelta(minutes=initial_delay_minutes)
    else:
        due = now - timedelta(seconds=60)
    sch = EvalSchedule(
        id=new_uuid(), suite_id=suite_id, tier=tier, cadence=cadence,
        enabled=enabled, next_due_at=due, owner_id=owner_id,
    )
    db.add(sch)
    db.commit()
    return _schedule_as_dict_with_suite(suite, sch)


def _schedule_as_dict(s: EvalSchedule) -> dict:
    return {
        "id": s.id, "suite_id": s.suite_id, "tier": s.tier,
        "cadence": s.cadence, "enabled": bool(s.enabled),
        "next_due_at": s.next_due_at.isoformat() if s.next_due_at else None,
        "last_fired_at": s.last_fired_at.isoformat() if s.last_fired_at else None,
        "last_run_id": s.last_run_id,
        "owner_id": s.owner_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _schedule_as_dict_with_suite(suite: EvalSuite, sch: EvalSchedule) -> dict:
    item = _schedule_as_dict(sch)
    item["suite_name"] = suite.name
    item["suite_version"] = suite.version
    return item


def list_schedules(db: Session, *, enabled_only: bool = False) -> dict:
    q = db.query(EvalSchedule).order_by(EvalSchedule.created_at.desc())
    if enabled_only:
        q = q.filter_by(enabled=True)
    rows = q.all()
    suites = {s.id: s for s in db.query(EvalSuite).all()}
    items = [_schedule_as_dict_with_suite(suites[r.suite_id], r)
             if r.suite_id in suites else _schedule_as_dict(r)
             for r in rows]
    return {"total": len(items), "items": items}


def get_schedule(db: Session, schedule_id: str) -> dict | None:
    sch = db.get(EvalSchedule, schedule_id)
    if sch is None:
        return None
    suite = db.get(EvalSuite, sch.suite_id)
    return _schedule_as_dict_with_suite(suite, sch) if suite else _schedule_as_dict(sch)


def set_schedule_enabled(db: Session, schedule_id: str, *, enabled: bool,
                         owner_id: str | None = None) -> dict:
    """启用/停用调度。停用不影响已触发的 Run；重新启用重新排期。"""
    sch = db.get(EvalSchedule, schedule_id)
    if sch is None:
        raise KeyError(schedule_id)
    if sch.enabled == enabled:
        return get_schedule(db, schedule_id) or {}
    sch.enabled = enabled
    if enabled:
        sch.next_due_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return get_schedule(db, schedule_id) or {}


def due_schedules(db: Session, *, limit: int = 20) -> list[dict]:
    """到期且启用的调度（供调度 Worker 拉取执行）。永远只返回 enabled + 未过期。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = (db.query(EvalSchedule)
            .filter(EvalSchedule.enabled.is_(True))
            .filter(EvalSchedule.next_due_at.isnot(None))
            .filter(EvalSchedule.next_due_at <= now)
            .order_by(EvalSchedule.next_due_at.asc())
            .limit(limit).all())
    suites = {s.id: s for s in db.query(EvalSuite).all()}
    return [_schedule_as_dict_with_suite(suites[r.suite_id], r)
            for r in rows if r.suite_id in suites]


def mark_schedule_fired(db: Session, schedule_id: str, *, run_id: str,
                        next_due: datetime | None = None) -> dict:
    """调度触发后落 last_fired + run 引用 + 重排 next_due。幂等记录。"""
    sch = db.get(EvalSchedule, schedule_id)
    if sch is None:
        raise KeyError(schedule_id)
    sch.last_fired_at = datetime.now(timezone.utc).replace(tzinfo=None)
    sch.last_run_id = run_id
    if next_due is not None:
        sch.next_due_at = next_due
    db.commit()
    return get_schedule(db, schedule_id) or {}


def suite_tier_cases(db: Session, suite_id: str, tier: str) -> list[str] | None:
    """返回 suite 的分层 case id 列表；suite/分层不存在返回 None（不炸调用方）。"""
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        return None
    tier_map = suite.tier_map or {}
    ids = tier_map.get(tier)
    if ids is None:
        return None
    return list(ids)


# =====================================================================
# V2.0 -2 评测中心产品化（Phase 2）：
# 数据集分类 / 运行进度 / 数据集版本对比 / 触发回归 / 定时评测 / 失败告警
# =====================================================================


# ---- 数据集分类：幂等写入 dataset_category_map（add-only，不改 case 内容）----

def sync_suite_categories(db: Session, suite_id: str) -> dict:
    """为 Suite 幂等复算类别映射（核心/边界/异常/并发/安全/语音）。

    按 (suite_id, case_ref_id) upsert，与 refresh_dataset 同一分类语义；
    不入数据库的不改任何 case 字段。返回 inserted/skipped 计数。
    """
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise KeyError(suite_id)
    inserted = skipped = 0
    for row in db.query(EvalCase).filter_by(suite_id=suite_id).all():
        cat, _reason = classify_case({
            "case_type": row.case_type,
            "tags": row.tags or [],
        })
        exists = (db.query(DatasetCategoryMap)
                  .filter_by(suite_id=suite_id, case_ref_id=row.case_ref_id).first())
        if exists:
            skipped += 1
            continue
        db.add(DatasetCategoryMap(
            suite_id=suite_id, case_ref_id=row.case_ref_id, category=cat))
        inserted += 1
    db.commit()
    return {"suite_id": suite_id, "inserted": inserted, "skipped": skipped}


def category_distribution(db: Session, suite_id: str | None = None) -> dict:
    """数据集类别分布（供评测中心按类别筛选页面）。"""
    from sqlalchemy import func as _func
    q = db.query(DatasetCategoryMap.category, _func.count(DatasetCategoryMap.id))
    if suite_id:
        q = q.filter_by(suite_id=suite_id)
    rows = q.group_by(DatasetCategoryMap.category).all()
    dist = {c: 0 for c in V2_DATASET_CATEGORIES}
    for cat, n in rows:
        if cat in dist:
            dist[cat] = n
        else:
            dist.setdefault(str(cat), n)
    return {"total": sum(dist.values()), "categories": dist}


# ---- 运行进度（页面轮询：已完成/总数）----

def expected_case_count(db: Session, run: EvalRun) -> int:
    """按 run config（tier/limit）估算预期用例数，用于进度分母。"""
    cfg = run.config or {}
    sid = run.suite_id or ""
    total = db.query(EvalCase).filter_by(suite_id=sid).count() if sid else 0
    if cfg.get("tier"):
        ids = suite_tier_cases(db, sid, cfg["tier"])
        if ids is not None:
            total = len(ids)
    lim = int(cfg.get("limit") or 0)
    if lim:
        total = min(total, lim)
    return total


def run_progress(db: Session, run_id: str) -> dict:
    """Run 实时进度（done/total + 来源 + 状态）。run 不存在抛 KeyError。"""
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        raise KeyError(run_id)
    done = db.query(EvalCaseResult).filter_by(run_id=run_id).count()
    total = expected_case_count(db, run)
    return {
        "run_id": run_id, "status": run.status,
        "suite_id": run.suite_id, "suite_version": run.suite_version,
        "trigger_source": run.trigger_source,
        "total_cases": total, "done_cases": done,
        "ratio": round(done / total, 4) if total else 0.0,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


# ---- 数据集版本对比（复制建新版后差异看板）----

def _case_fingerprint(row: EvalCase) -> tuple:
    return (row.case_type, row.category,
            json.dumps(row.inputs, sort_keys=True, ensure_ascii=False),
            json.dumps(row.expected, sort_keys=True, ensure_ascii=False),
            json.dumps(row.constraints, sort_keys=True, ensure_ascii=False))


def compare_suite_versions(db: Session, base_id: str, target_id: str) -> dict:
    """数据集版本对比：added/removed/changed 逐 case 差异 + 类别分布变化。

    两个 Suite 都必须存在（否则抛 KeyError）。基线只读，本对比零副作用。
    """
    base = db.get(EvalSuite, base_id)
    target = db.get(EvalSuite, target_id)
    if base is None:
        raise KeyError(base_id)
    if target is None:
        raise KeyError(target_id)

    def _index(sid: str) -> dict[str, EvalCase]:
        return {r.case_ref_id: r for r in db.query(EvalCase).filter_by(suite_id=sid).all()}

    bc, tc = _index(base_id), _index(target_id)
    added = sorted(set(tc) - set(bc))
    removed = sorted(set(bc) - set(tc))
    changed = [cid for cid in sorted(set(bc) & set(tc))
               if _case_fingerprint(bc[cid]) != _case_fingerprint(tc[cid])]
    unchanged = len(set(bc) & set(tc)) - len(changed)
    if added or removed or changed:
        verdict = "modified"
    else:
        verdict = "identical"
    return {
        "base": _suite_as_dict(base), "target": _suite_as_dict(target),
        "base_cases": len(bc), "target_cases": len(tc),
        "added_cases": added, "removed_cases": removed,
        "changed_cases": changed, "unchanged_cases": unchanged,
        "base_categories": category_distribution(db, base_id),
        "target_categories": category_distribution(db, target_id),
        "verdict": verdict,
    }


# ---- 触发回归（代码/Prompt/模型/策略变更 → 自动触发回归 Run）----

def trigger_regression(db: Session, *, trigger_source: str, reason: str = "",
                       suite_id: str | None = None, created_by: str | None = None,
                       mode: str = "in_process", limit: int = 0) -> dict:
    """代码/Prompt/模型/策略任一变更 → 在目标数据集上触发一次回归 Run。

    - 来源必须是 CHANGE_TRIGGER_SOURCES（非手动/定时）；来源直接落 run.trigger_source；
    - Suite 未指定时选最新 ACTIVE 数据集（回归跑当前基线）；
    - 防抖：同 suite+来源且存在 PENDING/RUNNING 回归 Run 时不重复入队（返回 dedup）。
    - 只创建 Run 并入队，绝不自动改任何业务决策/退款（评测隔离红线）。
    """
    if trigger_source not in CHANGE_TRIGGER_SOURCES:
        raise ValueError(f"trigger_source 必须为 {'/'.join(CHANGE_TRIGGER_SOURCES)}")
    suite = None
    if suite_id:
        suite = db.get(EvalSuite, suite_id)
        if suite is None:
            raise KeyError(suite_id)
    else:
        suite = (db.query(EvalSuite).filter_by(status=EVAL_SUITE_ACTIVE)
                 .order_by(EvalSuite.created_at.desc()).first())
        if suite is None:
            raise KeyError("无活动数据集，无法触发回归")
    existing = (db.query(EvalRun)
                .filter(EvalRun.suite_id == suite.id,
                        EvalRun.trigger_source == trigger_source,
                        EvalRun.status.in_([EVAL_RUN_PENDING, EVAL_RUN_RUNNING]))
                .first())
    if existing is not None:
        return {"run_id": existing.run_id, "trigger_source": trigger_source,
                "suite_id": suite.id, "status": existing.status, "dedup": True}
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    config = {"mode": mode, "limit": limit, "reason": reason,
              "trigger_source": trigger_source, "regression": True}
    run = create_run(db, run_id=run_id,
                     name=f"[回归:{trigger_source}] {suite.name}",
                     suite=suite, created_by=created_by, config=config,
                     trigger_source=trigger_source)
    from app.infrastructure.redis_client import STREAM_EVAL_RUN, xadd  # 延迟导入避免 import 期连 Redis
    xadd(STREAM_EVAL_RUN, {
        "case_id": run_id, "run_id": run_id, "suite_id": suite.id,
        "limit": str(limit), "priority": "0", "target_type": "refund_workflow",
        "trigger_source": trigger_source,
        "config": json.dumps(config, ensure_ascii=False),
    })
    return {"run_id": run_id, "trigger_source": trigger_source,
            "suite_id": suite.id, "status": run.status, "dedup": False}


# ---- 定时评测：到期自动触发 Run（daily/weekly/change；manual 仅供人工触发，绝不自动）----

def fire_due_schedules(db: Session, *, created_by: str = "scheduler",
                       limit: int = 10) -> dict:
    """Poll 一次：把所有「到期且启用」的 daily/weekly/change 调度触发为回归 Run。

    - manual 调度跳过（自动哨兵不该替人工跑）；
    - 触发后 mark_schedule_fired 落 last_run_id + 重排 next_due（幂等不重发）。
    """
    from app.infrastructure.redis_client import STREAM_EVAL_RUN, xadd  # 延迟导入
    fired = []
    for sch in due_schedules(db, limit=limit):
        if sch.get("cadence") == "manual":
            continue                    # 人工触发专用的调度，不自动跑
        suite = db.get(EvalSuite, sch["suite_id"])
        if suite is None:
            continue
        is_intent = sch["tier"] == "intent"
        if is_intent:
            # 意图对照是独立评测任务，不走退款 suite tier_map。
            # 仍复用通用 EvalRun + Redis Stream，保留同一套调度证据链。
            from app.services import intent_periodic_eval as ipe
            # 手动评测尚在执行时，保留 next_due 不动；下轮轮询会在
            # 活动 Run 结束后再触发，避免对同一数据集并发重复评测。
            if ipe.active_intent_run(db) is not None:
                continue
            run = ipe.create_intent_eval_run(
                db, created_by=created_by, trigger_source="schedule",
                schedule_id=sch["id"], suite=suite,
            )
            run_id = run.run_id
            config = run.config or {}
            target_type = "intent_router"
        else:
            run_id = f"run-{uuid.uuid4().hex[:12]}"
            config = {"mode": "in_process", "tier": sch["tier"],
                      "schedule_id": sch["id"], "regression": False}
            run = create_run(db, run_id=run_id,
                             name=f"[定评:{sch['tier']}/{sch['cadence']}] {suite.name}",
                             suite=suite, created_by=created_by, config=config,
                             trigger_source="schedule")
            target_type = "refund_workflow"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        mark_schedule_fired(db, sch["id"], run_id=run_id,
                            next_due=_next_due(str(sch.get("cadence") or "daily"), now))
        xadd(STREAM_EVAL_RUN, {
            "case_id": run_id, "run_id": run_id, "suite_id": suite.id,
            "limit": "0", "priority": "0", "target_type": target_type,
            "trigger_source": "schedule",
            "config": json.dumps(config, ensure_ascii=False),
        })
        fired.append({"schedule_id": sch["id"], "run_id": run_id,
                      "tier": sch["tier"], "cadence": sch["cadence"]})
    return {"count": len(fired), "fired": fired}


# ---- 失败告警：定时评测触发的 Run 失败 → EvalAlert（RUN_FAILED，幂等）----

def raise_run_failure_alerts(db: Session, *, limit: int = 50) -> dict:
    """扫描启用调度的 last_run：若该 Run FAILED 且未告警过 → 落 RUN_FAILED 告警。

    只写 EvalAlert（观测红线：绝不改退款/评测决策）；ack 后不再重复提示。
    """
    created: list[dict] = []
    for sch in db.query(EvalSchedule).filter(EvalSchedule.enabled.is_(True)).limit(limit).all():
        rid = sch.last_run_id
        if not rid:
            continue
        run = db.query(EvalRun).filter_by(run_id=rid).first()
        if run is None or run.status != EVAL_RUN_FAILED:
            continue
        exists = (db.query(EvalAlert)
                  .filter_by(alert_type="RUN_FAILED", run_id=rid).first())
        if exists:
            continue
        db.add(EvalAlert(
            id=new_uuid(), alert_type="RUN_FAILED", run_id=rid,
            suite_id=sch.suite_id,
            # metric_key 列 VARCHAR(40)：uuid 36 位过长，取纯时段前缀；run 全量在 run_id 列
            metric_key=f"schedule/{sch.cadence}/{sch.tier}:{sch.id[:18]}",
            message=f"定时评测失败 [{sch.tier}/{sch.cadence}] {rid}：{(run.error or '')[:240]}",
            severity="error"))
        created.append({"schedule_id": sch.id, "run_id": rid})
    db.commit()
    return {"count": len(created), "alerts": created}


def _alert_as_dict(a: EvalAlert) -> dict:
    return {
        "id": a.id, "alert_type": a.alert_type, "run_id": a.run_id,
        "suite_id": a.suite_id, "metric_key": a.metric_key,
        "message": a.message, "severity": a.severity,
        "acked": bool(a.acked), "acked_by": a.acked_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def list_alerts(db: Session, *, acked: bool | None = None, limit: int = 50) -> dict:
    """告警列表（默认全部；acked=True/False 过滤）。"""
    q = db.query(EvalAlert).order_by(EvalAlert.created_at.desc())
    if acked is not None:
        q = q.filter(EvalAlert.acked.is_(acked))
    rows = q.limit(limit).all()
    return {"total": len(rows), "items": [_alert_as_dict(r) for r in rows]}


def ack_alert(db: Session, alert_id: str, *, acked_by: str | None = None) -> dict:
    """处理一条失败告警（ack），此后不再重复提示该 run/metric。"""
    a = db.get(EvalAlert, alert_id)
    if a is None:
        raise KeyError(alert_id)
    a.acked = True
    a.acked_by = acked_by
    db.commit()
    return _alert_as_dict(a)
