# -*- coding: utf-8 -*-
"""评测报告服务（M13）：历史 V1 报告读取 + Run↔Run 对比。

兼容说明：report_version 1.0 由 V1（数据集 1.0.0，15 条）与 V2（2.0.0，30 条）共用
同一报告 schema，因此历史 V1 `reports/*.json` 可直接按同构读取，无需迁移。

读取路径：
- 优先 reports/<run_id>.json（权威归档；V1 与 Worker 归档都走这条）；
- 未找到则从 DB（EvalRun + EvalCaseResult）重建（M12 之后的新 Run）。

对比（compare_runs）：质量 pass_rate / P95 / 状态 / 成本实测 / 失败差异。
只对两侧「共同 case」做案例级 diff，绝不把未测当通过；未知判定按 degraded 处理。
红线：degraded / ERROR / SKIPPED_UNSUPPORTED 一律不视为 pass。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.domain.models import EvalRun
from app.services.eval_center import list_run_results

_REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"


# ---- 判定归一（pass/fail/degraded/skipped）----

def _norm_verdict(v: Any) -> str:
    """把报告里的判定字符串归一到 pass/fail/degraded/skipped。"""
    up = str(v or "").upper()
    if up.startswith("PASS"):
        return "pass"
    if up.startswith("FAIL"):
        return "fail"
    if up.startswith("SKIPPED"):
        return "skipped"
    return "degraded"  # DEGRADED/ERROR/未知：一律不视为通过


def _verdict_to_judge(stored: str) -> str:
    """DB 存储判定 -> judge 字符串。"""
    up = (stored or "").upper()
    if up == "PASS":
        return "pass"
    if up == "FAIL":
        return "fail"
    if up == "SKIPPED_UNSUPPORTED":
        return "skipped"
    return "degraded"


# ---- 读取 ----

def _normalize(doc: dict) -> dict:
    return doc


def read_report(db: Session, run_id: str) -> dict:
    """读取一份报告：优先归档文件，其次 DB 重建。run_id 不存在抛 KeyError。"""
    p = _REPORTS_DIR / f"{run_id}.json"
    if p.exists():
        return _normalize(json.loads(p.read_text(encoding="utf-8")))
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        raise KeyError(run_id)
    return _normalize(_report_from_run(db, run))


def _report_from_run(db: Session, run: EvalRun) -> dict:
    results = list_run_results(db, run.run_id)
    cases = []
    for r in results:
        verdict = _verdict_to_judge(r["verdict"])
        cases.append({
            "case_id": r["case_id"],
            "case_type": r["case_type"],
            "schema_version": "1.0",
            "execution": r.get("execution") or {},
            "judge": {
                "verdict": verdict,
                "display": r["verdict"],
                "dimensions": r.get("dimensions") or {},
                "evidence": r.get("evidence") or {},
                "llm": r.get("llm_judge"),
            },
            "candidate": r.get("candidate"),
            "degraded": r.get("degraded", False),
        })
    summary = dict(run.summary or {})
    summary.setdefault("pass_rate",
                       round(summary.get("passed", 0) / summary.get("total", 0), 4)
                       if summary.get("total") else 0.0)
    return {
        "report_version": "1.0",
        "run_id": run.run_id,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "environment": {
            "dataset_version": run.suite_version,
            "dataset_checksum": run.dataset_checksum,
        },
        "dataset": {"version": run.suite_version, "checksum": run.dataset_checksum,
                    "case_count": summary.get("total", 0)},
        "summary": summary,
        "performance": run.performance or {},
        "cost": run.cost or {},
        "extensions": {"security": None, "intent": None, "voice": None, "coaching": None},
        "trace": {"trace_id": run.run_id, "run_id": run.run_id},
        "cases": cases,
        "_source": "db",
    }


def list_reports(db: Session) -> list[dict]:
    """列出归档报告（reports/*.json）。含 run_id / 数据集版本 / 质量摘要 / status=ARCHIVED。"""
    out: list[dict] = []
    for p in sorted(_REPORTS_DIR.glob("run-*.json"), reverse=True):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        s = doc.get("summary") or {}
        out.append({
            "run_id": doc.get("run_id") or p.stem,
            "report_version": doc.get("report_version"),
            "created_at": doc.get("created_at"),
            "dataset_version": (doc.get("environment") or {}).get("dataset_version"),
            "dataset_checksum": (doc.get("environment") or {}).get("dataset_checksum"),
            "total": s.get("total"),
            "passed": s.get("passed"),
            "failed": s.get("failed"),
            "degraded": s.get("degraded"),
            "pass_rate": s.get("pass_rate"),
            "cost_measured": bool((doc.get("cost") or {}).get("measured")),
            "status": "ARCHIVED",
        })
    # 补充 DB 中未归档的 Run（取消/失败等无报告文件者）
    seen = {r["run_id"] for r in out}
    for run in (db.query(EvalRun)
                .filter(or_(EvalRun.target_type.is_(None),
                            EvalRun.target_type != "intent_router"))
                .order_by(EvalRun.created_at.desc()).all()):
        if run.run_id in seen:
            continue
        s = run.summary or {}
        out.append({
            "run_id": run.run_id,
            "report_version": "1.0",
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "dataset_version": run.suite_version,
            "dataset_checksum": run.dataset_checksum,
            "total": s.get("total"), "passed": s.get("passed"),
            "failed": s.get("failed"), "degraded": s.get("degraded"),
            "pass_rate": s.get("pass_rate"),
            "cost_measured": bool((run.cost or {}).get("measured")),
            "status": run.status,
        })
    return out


# ---- 对比 ----

def _snapshot(doc: dict) -> dict:
    s = doc.get("summary") or {}
    perf = (doc.get("performance") or {}).get("latency_ms") or {}
    return {
        "run_id": doc.get("run_id"),
        "report_version": doc.get("report_version"),
        "dataset_version": (doc.get("environment") or {}).get("dataset_version"),
        "created_at": doc.get("created_at"),
        "total": s.get("total"), "passed": s.get("passed"),
        "failed": s.get("failed"), "degraded": s.get("degraded"),
        "skipped": s.get("skipped") or s.get("skipped_unsupported") or 0,
        "pass_rate": s.get("pass_rate"),
        "p95": perf.get("p95"),
        "cost_measured": bool((doc.get("cost") or {}).get("measured")),
    }


def compare_runs(db: Session, run_a: str, run_b: str) -> dict:
    """对比两份报告。只对共同 case 做案例级 diff；degraded/skipped 不视为通过。"""
    a = read_report(db, run_a)
    b = read_report(db, run_b)
    av = {c.get("case_id"): _norm_verdict((c.get("judge") or {}).get("verdict"))
          for c in a.get("cases", [])}
    bv = {c.get("case_id"): _norm_verdict((c.get("judge") or {}).get("verdict"))
          for c in b.get("cases", [])}
    common = sorted(set(av) & set(bv))
    regressed = [cid for cid in common if av[cid] == "pass" and bv[cid] != "pass"]
    improved = [cid for cid in common if av[cid] != "pass" and bv[cid] == "pass"]
    ra = a.get("summary", {}).get("pass_rate")
    rb = b.get("summary", {}).get("pass_rate")
    delta = (rb - ra) if (ra is not None and rb is not None) else None
    # 无共同 case 时（如 DB 重建无逐 case 结果）退化为摘要 pass_rate 对比
    if delta is None:
        verdict = "incomparable"
    elif delta > 0.0001:
        verdict = "improved"
    elif delta < -0.0001:
        verdict = "regressed"
    else:
        verdict = "unchanged"
    return {
        "run_a": _snapshot(a),
        "run_b": _snapshot(b),
        "deltas": {
            "pass_rate": round(delta, 4) if delta is not None else None,
            "common_cases": len(common),
            "regressed_count": len(regressed),
            "improved_count": len(improved),
        },
        "regressed": regressed,
        "improved": improved,
        "verdict": verdict,
    }


def _cost_snapshot(doc: dict) -> dict:
    c = doc.get("cost") or {}
    return {
        "measured": bool(c.get("measured")),
        "measured_cases": c.get("measured_cases") or 0,
        "input_tokens_total": c.get("input_tokens_total") or 0,
        "output_tokens_total": c.get("output_tokens_total") or 0,
        "cost_total": c.get("cost_total") or 0.0,
    }


def compare_a_b(db: Session, run_a: str, run_b: str) -> dict:
    """A-B：同一数据集 baseline vs candidate 对比（质量 + token/成本 Δ）。

    判定直觉：
    - pass delta 定义为 variant - baseline（>0 候选更优）；
    - token 降幅 ≥30% 且质量不劣化 → 通过（adopted=deploy）；
    - 质量劣化（variant < baseline）→ 否决，无论省多少 token；
    - 任一侧未实测（measured=False）→ token_delta 不可比，作为“未测量”如实呈现。
    企业红线：未测量绝不当作“达标”。
    """
    a = read_report(db, run_a)   # baseline
    b = read_report(db, run_b)   # variant

    # 质量 pass_rate delta（variant - baseline）
    ra = a.get("summary", {}).get("pass_rate")
    rb = b.get("summary", {}).get("pass_rate")
    rate_delta = (rb - ra) if (ra is not None and rb is not None) else None

    # case 级（只算共同 case；degraded/skipped 不视为 pass）
    av = {c.get("case_id"): _norm_verdict((c.get("judge") or {}).get("verdict"))
          for c in a.get("cases", [])}
    bv = {c.get("case_id"): _norm_verdict((c.get("judge") or {}).get("verdict"))
          for c in b.get("cases", [])}
    common = sorted(set(av) & set(bv))
    regressed = [cid for cid in common if av[cid] == "pass" and bv[cid] != "pass"]
    improved = [cid for cid in common if av[cid] != "pass" and bv[cid] == "pass"]

    ca = (_cost_snapshot(a).get("input_tokens_total", 0)
          + _cost_snapshot(a).get("output_tokens_total", 0))
    cb = (_cost_snapshot(b).get("input_tokens_total", 0)
          + _cost_snapshot(b).get("output_tokens_total", 0))
    ma = bool((a.get("cost") or {}).get("measured"))
    mb = bool((b.get("cost") or {}).get("measured"))

    measured = bool(ma and mb)
    if measured:
        reduction = float(ca - cb) / float(ca) if ca else 0.0
        token_delta_pct = round(reduction * 100, 2)
        quality_ok = (rate_delta or 0) >= -1e-9
        adopted = bool(token_delta_pct >= 30.0 and quality_ok)
    else:
        token_delta_pct = None
        adopted = False

    return {
        "baseline": _snapshot(a), "variant": _snapshot(b),
        "cost_a": _cost_snapshot(a), "cost_b": _cost_snapshot(b),
        "measured": measured,
        "token_delta_pct": token_delta_pct,
        "token_reduction_goal_30pct": (token_delta_pct or 0) >= 30.0,
        "rate_delta": round(rate_delta, 4) if rate_delta is not None else None,
        "quality_ok": (rate_delta or 0) >= -1e-9 if rate_delta is not None else None,
        "regressed": regressed, "improved": improved,
        "pass": adopted,
        # 未测量/不可比时一律不部署：红线
        "deployable": bool(measured and adopted and not regressed),
    }
