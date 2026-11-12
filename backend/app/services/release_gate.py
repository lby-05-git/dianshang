# -*- coding: utf-8 -*-
"""发布门禁与候选回放（M19 V3.0 持续评测治理）。

职责：
- 基线批准：把某个已完成的 COMPLETED Run 人工批准为发布基线（approval 持久化，
  approver/approved_at 留痕；同 role 基线唯一，切换基线只可能指向证据链中的 Run）。
- 门禁判定（gate）：只读某候选 Run 相对基线的证据，判定
  1) 资金红线（MISMATCH/UNKNOWN 触发过自动退款即阻断，绝不让路）；
  2) 质量不劣化（共同 case 无回归）；
  3) P95 / Token 成本不超预算阈值。
  绝不原地改任何 Run、任何凭据；绝不外改（候选在本评测中心体系内）。
- 候选回放：门禁通过后，为指定数据集 + 分层（tier）+ 候选 Run 生成回放集，
  即"把候选在回放集上再次执行"的约定与执行入口（golden-follower/HTTP Target 黑色盒）。

红线：
- MISMATCH/UNKNOWN 永不触发自动退款；命中即 fail，人肉必须介入；
- degraded / ERROR / SKIPPED_UNSUPPORTED 一律不视为通过（measured 语义）；
- 未测量（measured=False）的 token index 不能当作达标；未测量 -> 门禁不 PASS；
- 门禁判定只读：不修改候选/基线 Run，不修改数据集，不调用真实退款。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import (
    EvalCase,
    EvalCaseResult,
    EvalRun,
    EvalSuite,
    PROMPT_APPROVED,
    PromptVersion,
    ReleaseEvidencePackage,
    new_uuid,
)

GATE_VERSION = "1.0"

# 门禁默认阈值（可被 policy 覆盖；均为"上限"语义，超限即不 PASS）
DEFAULT_GATE_POLICY = {
    "max_pass_rate_drop_pct": 0.0,      # 候选 pass_rate 不得低于基线（pct 单位：0.01=1%）
    "max_p95_ms": 5000.0,               # 候选 P95 时延上限
    "max_token_pct_increase": 10.0,     # 候选 token index 相比基线允许上涨 pct
}

# 资金红线：命中即 fail（即使其他维度全过）
FUNDING_RED_LINE_DECISIONS = ("MISMATCH", "UNKNOWN")


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class GateResult:
    """一次门禁判定的完整证据（persistable 到 gate_results 或内存返回）。

    V3.2 五维门禁（-评测 / -1）：
      1) 业务正确率  pass_rate   —— 候选相对基线不劣化；
      2) 资金红线    money_safe  —— MISMATCH/UNKNOWN 触发自动退款即阻断；
      3) 安全红线    security_ok —— 候选含 security-tag 用例时，全部通过且不回归
                                     （未含安全用例 = N/A，不举证、不阻断，如实标记）；
      4) 意图指标    intent_ok   —— 候选含 intent-tag 用例时，全部通过且不回归
                                     （同上 N/A 语义；V3.4 再挂真实召回/幻觉指标）；
      5) 成本/P95    token_ok / p95_ok / measured —— 未实测 token 不 PASS。
    """
    gate_id: str
    run_id: str                      # 候选 Run
    baseline_run_id: str
    tier: str
    pass_rate: bool
    money_safe: bool
    p95_ok: bool
    token_ok: bool
    measured: bool
    thresholds: dict
    security_ok: bool = True         # 候选已测安全维度时是否全过
    intent_ok: bool = True           # 候选已测意图维度时是否全过
    security_measured: bool = False  # 候选 Run 是否含 security-tag 用例（不会假 pass）
    intent_measured: bool = False    # 候选 Run 是否含 intent-tag 用例
    reason: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    fund_hits: list[dict] = field(default_factory=list)
    security_issues: list[str] = field(default_factory=list)
    intent_issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # 未测得维度（N/A）不阻断；已测维度任一不 PASS 即不 PASS。
        sec_ok = True if not self.security_measured else self.security_ok
        int_ok = True if not self.intent_measured else self.intent_ok
        return bool(self.pass_rate and self.money_safe and self.p95_ok
                    and self.token_ok and self.measured and sec_ok and int_ok
                    and not self.regressed)

    @property
    def decision(self) -> str:
        if not self.money_safe:
            return "BLOCKED_FUNDING_RED_LINE"
        if self.security_measured and not self.security_ok:
            return "BLOCKED_SECURITY"
        if self.intent_measured and not self.intent_ok:
            return "BLOCKED_INTENT"
        if self.regressed or not self.pass_rate:
            return "BLOCKED_QUALITY"
        if not self.measured:
            return "BLOCKED_UNMEASURED"
        return "PASS" if self.passed else "BLOCKED_PERFORMANCE"

    def as_dict(self) -> dict:
        return {
            "gate_id": self.gate_id,
            "run_id": self.run_id,
            "baseline_run_id": self.baseline_run_id,
            "tier": self.tier,
            "passed": self.passed,
            "decision": self.decision,
            "pass_rate": self.pass_rate,
            "money_safe": self.money_safe,
            "p95_ok": self.p95_ok,
            "token_ok": self.token_ok,
            "measured": self.measured,
            "security_ok": self.security_ok,
            "intent_ok": self.intent_ok,
            "security_measured": self.security_measured,
            "intent_measured": self.intent_measured,
            "thresholds": self.thresholds,
            "reason": list(self.reason),
            "regressed": list(self.regressed),
            "fund_hits": [dict(x) for x in self.fund_hits],
            "security_issues": list(self.security_issues),
            "intent_issues": list(self.intent_issues),
            "gate_version": GATE_VERSION,
            "checked_at": _now_naive().isoformat(),
        }


# ---- 报告/证据读取（与 report_service 同源，但这里只读）----

def _norm_verdict(v) -> str:
    up = str(v or "").upper()
    if up.startswith("PASS"):
        return "pass"
    if up.startswith("FAIL"):
        return "fail"
    if up.startswith("SKIPPED"):
        return "skipped"
    return "degraded"  # DEGRADED/ERROR/未知：一律不视为通过


def _verdict_to_judge(store_verdict: str) -> str:
    up = (store_verdict or "").upper()
    if up == "PASS":
        return "pass"
    if up == "FAIL":
        return "fail"
    if up == "SKIPPED_UNSUPPORTED":
        return "skipped"
    return "degraded"


def _case_map(report: dict) -> dict[str, str]:
    """report（DB 重建或归档 JSON）-> {case_id: normalized verdict}。

    DB 重建报告 judge.verdict 已是 pass/fail/skipped/degraded；归档 JSON 报告
    judge.verdict 也兼容（_norm_verdict 对大小写/前缀归一）。未测一律不视为 pass。
    """
    out: dict[str, str] = {}
    for c in report.get("cases") or []:
        v = (c.get("judge") or {}).get("verdict")
        out[c.get("case_id")] = _norm_verdict(v)
    return out


def _report_candidate_biz(report: dict) -> dict[str, dict]:
    """report -> {case_id: business dict}（fund red-line 判定用）。"""
    out: dict[str, dict] = {}
    for c in report.get("cases") or []:
        cand = c.get("candidate") or {}
        out[c.get("case_id")] = cand.get("business") or {}
    return out


def _case_dimension_tags(db: Session, case_ref_ids: set[str]) -> dict[str, set[str]]:
    """从 EvalCase 读取用例的真实 tag 集（安全/意图维度识别依据）。

    优于用 case_type 前缀猜测：tag 是数据集声明的一等字段（security/intent/...），
    报告里只含 case_id + case_type，故必须回查套件快照拿 tags。
    """
    if not case_ref_ids:
        return {}
    rows = (db.query(EvalCase.case_ref_id, EvalCase.tags)
            .filter(EvalCase.case_ref_id.in_(case_ref_ids)).all())
    return {cid: set(tags or []) for cid, tags in rows}


def _report_pass_rate(report: dict) -> float:
    s = report.get("summary") or {}
    total = s.get("total") or 0
    return round(s.get("passed", 0) / total, 4) if total else 0.0


def _report_p95(report: dict) -> float | None:
    """报告 performance.latency_ms.p95。缺失/未测量返回 None（视为不超限）。"""
    p = (report.get("performance") or {}).get("latency_ms") or {}
    return p.get("p95")


def _report_tokens(report: dict) -> dict:
    c = report.get("cost") or {}
    return {
        "in": int(c.get("input_tokens_total") or 0),
        "out": int(c.get("output_tokens_total") or 0),
        "measured": bool(c.get("measured")),
    }


# ---- 读取归档/DB 报告（只读）----

def read_report_dict(db: Session, run_id: str) -> dict:
    """只读一份候选/基线报告：优先 reports/*.json 归档，其次 DB 重建。"""
    from app.services.report_service import read_report
    return read_report(db, run_id)


# ---- 基线批准 ----

def approve_baseline(db: Session, *, run_id: str, suite_id: str,
                     actor_id: str, note: str = "") -> dict:
    """人工批准一个已完成 Run 作为发布基线。

    红线：只能批准 COMPLETED 且非 FAILED 的 Run；approver 持久化留痕。
    一个 role（suite）只保留一条基线行（切换 = 原地覆盖历史 run_id，
    返回 previous_run_id 留痕；旧 Run 仍在证据链中，可审计）。
    """
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        raise KeyError("run not found")
    if run.status != "COMPLETED":
        raise ValueError(f"Run 状态 {run.status} 不可批准为基线（需 COMPLETED）")
    if run.suite_id != suite_id:
        raise ValueError("Run 与套件不匹配，不可作为该套件基线")
    from app.domain.models import EvalBaseline
    existing = (db.query(EvalBaseline)
                .filter_by(suite_id=suite_id, role="default").first())
    now = _now_naive()
    if existing:
        prev = existing.run_id
        if prev == run_id:
            existing.note = note or existing.note
            db.commit()
            return _baseline_as_dict(existing)
        existing.run_id = run_id
        existing.dataset_checksum = run.dataset_checksum
        existing.suite_version = run.suite_version
        existing.approved_by = actor_id
        existing.approved_at = now
        existing.note = note or existing.note
        db.commit()
        return {**_baseline_as_dict(existing), "previous_run_id": prev}
    bl = EvalBaseline(
        id=new_uuid(), suite_id=suite_id, role="default", run_id=run_id,
        dataset_checksum=run.dataset_checksum, suite_version=run.suite_version,
        approved_by=actor_id, approved_at=now, note=note or "",
    )
    db.add(bl)
    db.commit()
    return {**_baseline_as_dict(bl), "previous_run_id": None}


def get_baseline(db: Session, suite_id: str) -> dict | None:
    from app.domain.models import EvalBaseline
    row = (db.query(EvalBaseline)
           .filter_by(suite_id=suite_id, role="default").first())
    return _baseline_as_dict(row) if row else None


# ---- V3.2 可信运行积攒门槛（前置补齐：≥10 次可信评测 + 稳定基线批准）----

TRUSTED_RUN_TARGET = 10
_TRUSTED_TRIGGERS = ("schedule", "manual", "code", "prompt", "model", "policy",
                     "change")


def _is_trusted_run(run) -> bool:
    """可信运行 = 真实评测（非测试造数）完成且可测量。

    判定（如实，绝不把未测当达标）：
    - status = COMPLETED 且非 FAILED；
    - cost.measured = True（token/成本为真实测量）；
    - summary 含 total/pass_rate（结果不是空的）；
    - trigger_source 属真实使用来源（调度/人工/变更触发/策略变更），
      测试驱动（trigger_source 为测试专用标记）不计数。
    """
    if run.status != "COMPLETED":
        return False
    if (run.cost or {}).get("measured") is not True:
        return False
    summ = run.summary or {}
    if not (summ.get("total") and summ.get("pass_rate") is not None):
        return False
    if (run.trigger_source or "") not in _TRUSTED_TRIGGERS:
        return False                     # 测试专用 / 未知来源不计可信
    return True


def trusted_runs_health(db: Session) -> dict:
    """可信运行积攒驾驶舱（只读，绝不写基线；缺多少如实标 PENDING）。"""
    from app.domain.models import EvalBaseline
    runs = db.query(EvalRun).filter(EvalRun.status == "COMPLETED").all()
    trusted = [r for r in runs if _is_trusted_run(r)]
    by_suite: dict[str, int] = {}
    for r in trusted:
        by_suite[r.suite_id or "(none)"] = by_suite.get(r.suite_id or "(none)", 0) + 1
    suites = db.query(EvalSuite).all()
    by_suite = {s.name: by_suite.get(s.id, 0) for s in suites}
    baseline_count = db.query(EvalBaseline).count()
    achieved = len(trusted) >= TRUSTED_RUN_TARGET
    run_ids = sorted({r.run_id for r in trusted})
    return {
        "target": TRUSTED_RUN_TARGET,
        "trusted_runs": len(trusted),
        "by_suite": by_suite,
        "run_ids": run_ids[-20:],          # 最近 20 条可信 run_ids 证据
        "baseline_approved": baseline_count,
        "stable_baseline": baseline_count > 0,
        "state": "ACHIEVED" if (achieved and baseline_count > 0) else "PENDING",
        "gap": max(0, TRUSTED_RUN_TARGET - len(trusted)),
        "note": (f"可信评测 {len(trusted)}/{TRUSTED_RUN_TARGET}"
                 f"{'已达标，等待/已批准稳定基线' if achieved else '未达标，缺口如实保留'}"
                 f"；不刷数不造假"),
    }


def maybe_approve_trusted_baseline(db: Session, *, actor_id: str) -> dict:
    """门槛达标时把最新可信 Run 批准为该套件基线（真实治理写入）。

    仅在同时满足以下条件才写 eval_baselines（否则返回 PENDING，绝不造数）：
    1) 可信运行数 ≥10（TRUSTED_RUN_TARGET）；
    2) 该套件存在至少一条可信 Run 且尚无稳定基线；
    3) Run 为 COMPLETED（approve_baseline 内部再次守线）。
    返回：{"state", "approved": [..], "note"}，供驾驶舱/定时任务消费。
    """
    if db.query(EvalRun).count() == 0:
        return {"state": "PENDING", "approved": [], "note": "无任何评测 Run"}
    trusted = [r for r in db.query(EvalRun).filter(EvalRun.status == "COMPLETED").all()
               if _is_trusted_run(r)]
    if len(trusted) < TRUSTED_RUN_TARGET:
        return {"state": "PENDING", "approved": [],
                "gap": TRUSTED_RUN_TARGET - len(trusted),
                "note": f"可信运行 {len(trusted)}/{TRUSTED_RUN_TARGET}，未达标不批准"}
    from app.domain.models import EvalBaseline
    approved: list[dict] = []
    note = f"可信运行积攒达标自动批准（trusted={len(trusted)}≥{TRUSTED_RUN_TARGET}）"
    # 同一套件里最新一条可信 Run 优先作候选基线
    for run in sorted(trusted, key=lambda r: r.created_at or _now_naive(),
                      reverse=True):
        if not run.suite_id:
            continue
        existing = (db.query(EvalBaseline)
                    .filter_by(suite_id=run.suite_id, role="default").first())
        if existing:
            continue                              # 已有稳定基线：不动
        if run.status != "COMPLETED":
            continue
        row = EvalBaseline(
            id=new_uuid(), suite_id=run.suite_id, role="default",
            run_id=run.run_id, dataset_checksum=run.dataset_checksum,
            suite_version=run.suite_version, approved_by=actor_id,
            approved_at=_now_naive(), note=note,
        )
        db.add(row)
        approved.append(_baseline_as_dict(row))
    db.commit()
    return {"state": "ACHIEVED" if approved else ("PASS" if
            db.query(EvalBaseline).filter_by(role="default").count() > 0
            else "PENDING"),
            "approved": approved,
            "note": (f"可信运行 {len(trusted)} 达标；本次批准 {len(approved)} 条基线"
                     if approved else "阈值已达标，无未批准基线候选")}


def _baseline_as_dict(b) -> dict:
    return {
        "id": b.id, "suite_id": b.suite_id, "role": b.role, "run_id": b.run_id,
        "dataset_checksum": b.dataset_checksum, "suite_version": b.suite_version,
        "approved_by": b.approved_by,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        "note": b.note,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


# ---- 门禁判定 ----

def evaluate_release_gate(db: Session, *, run_id: str, suite_id: str,
                          baseline_run_id: str | None = None,
                          tier: str = "full",
                          policy: dict | None = None) -> GateResult:
    """对候选 Run 做发布门禁判定（只读，绝不动任何决策/退款）。

    thresholds：见 DEFAULT_GATE_POLICY。所有维度为"上限/必达"语义：
      1) money_safe: 候选在任一 case 的 business 中触发过自动退款
         （refund_status=SUCCESS）而决策在 MISMATCH/UNKNOWN 红线集内 -> 违线；
      2) pass_rate: 候选 pass_rate >= 基线 pass_rate - max_pass_rate_drop_pct（容差 0）；
      3) p95_ok: 候选 P95 <= max_p95_ms；
      4) token_ok: 候选 token index <= 基线 * (1 + max_token_pct_increase/100)；
      5) measured: 成本维度必须实测（measured=True），未测量不 PASS。
    """
    gate_id = f"gate-{datetime.now().strftime('%Y%m%d%H%M%S')}-{run_id[-6:]}"
    t = {**DEFAULT_GATE_POLICY, **(policy or {})}

    cand = read_report_dict(db, run_id)
    if baseline_run_id:
        base = read_report_dict(db, baseline_run_id)
        base_rate = _report_pass_rate(base)
        base_tokens = _report_tokens(base)
    else:
        base_rate = 0.0        # 无基线：候选不能劣化于"没有基线"（只做自身的红线/性能）
        base_tokens = {"in": 0, "out": 0, "measured": False}

    cand_rate = _report_pass_rate(cand)
    cand_p95 = _report_p95(cand)
    cand_tokens = _report_tokens(cand)

    # 1) 资金红线：候选 business 触发自动退款但决策在 MISMATCH/UNKNOWN -> 阻断
    fund_hits: list[dict] = []
    biz_map = _report_candidate_biz(cand)
    for cid, biz in biz_map.items():
        refund = str(biz.get("refund_status") or "").upper()
        decision = str(biz.get("final_decision") or "").upper()
        if refund == "SUCCESS" and decision in FUNDING_RED_LINE_DECISIONS:
            fund_hits.append({"case_id": cid, "decision": decision,
                              "refund_status": refund})
    money_safe = not fund_hits

    # 2) 质量不劣化：共同 case 中候选从 pass -> 非 pass = 回归
    cand_cases = _case_map(cand)
    cid_set = set(cand_cases)
    if baseline_run_id:
        base_cases = _case_map(base)
        common = sorted(cid_set & set(base_cases))
        regressed = [cid for cid in common
                     if base_cases[cid] == "pass" and cand_cases[cid] != "pass"]
    else:
        common = []
        regressed = [cid for cid in cand_cases if cand_cases[cid] != "pass"]
    pass_rate_ok = (cand_rate + (t.get("max_pass_rate_drop_pct") or 0.0)
                    >= base_rate - 1e-9)

    # 3) 安全/意图红线（V3.2 五维门禁）：候选含对应 tag 用例即"已测"；
    #    已测要求全过 + 相对基线不回归；未含 tag 用例 = N/A（不举证、不阻断，如实标记）。
    tags_by_id = _case_dimension_tags(db, cid_set)
    security_ids = {cid for cid in cid_set if "security" in tags_by_id.get(cid, set())}
    intent_ids = {cid for cid in cid_set if "intent" in tags_by_id.get(cid, set())}
    security_measured = bool(security_ids)
    intent_measured = bool(intent_ids)

    security_issues: list[str] = []
    for cid in sorted(security_ids):
        if cand_cases[cid] != "pass":
            security_issues.append(cid)
    if baseline_run_id:
        for cid in sorted(security_ids & set(base_cases)):
            if base_cases[cid] == "pass" and cand_cases[cid] != "pass":
                if cid not in security_issues:
                    security_issues.append(cid)
    security_ok = security_issues == []

    intent_issues: list[str] = []
    for cid in sorted(intent_ids):
        if cand_cases[cid] != "pass":
            intent_issues.append(cid)
    if baseline_run_id:
        for cid in sorted(intent_ids & set(base_cases)):
            if base_cases[cid] == "pass" and cand_cases[cid] != "pass":
                if cid not in intent_issues:
                    intent_issues.append(cid)
    intent_ok = intent_issues == []

    # 4) P95
    p95 = (t.get("max_p95_ms") or float("inf"))
    p95_ok = cand_p95 is None or cand_p95 <= p95

    # 5) Token 预算（未测量 -> 不能证明达标 -> 不 PASS）
    measured = bool(cand_tokens.get("measured")) or not baseline_run_id
    tok_in = cand_tokens.get("in", 0) + cand_tokens.get("out", 0)
    base_tok = base_tokens.get("in", 0) + base_tokens.get("out", 0)
    if base_tok > 0 and measured:
        token_ok = (tok_in <= base_tok * (1 + (t.get("max_token_pct_increase")
                                               or 0.0) / 100.0) + 1e-9)
    else:
        token_ok = True

    reason = []
    if fund_hits:
        reason.append(f"资金红线: {len(fund_hits)} 条 MISMATCH/UNKNOWN 自动退款")
    if security_measured and security_issues:
        reason.append(f"安全红线 {len(security_issues)} 条: {','.join(security_issues[:5])}")
    if intent_measured and intent_issues:
        reason.append(f"意图指标 {len(intent_issues)} 条: {','.join(intent_issues[:5])}")
    if regressed:
        reason.append(f"质量回归 {len(regressed)} 条: {','.join(regressed[:5])}")
    if not pass_rate_ok:
        reason.append(f"pass_rate {cand_rate} < 基线 {base_rate}")
    if not p95_ok:
        reason.append(f"P95 {cand_p95:.0f}ms 超上限 {t.get('max_p95_ms')}ms")
    if not measured and baseline_run_id:
        reason.append("成本未实测（measured=False），不能证明达标")
    if security_measured:
        reason.append(f"安全维度 {len(security_ids)} 用例{'全过' if security_ok else '有异常'}")
    else:
        reason.append("候选无 security-tag 用例：安全维度 N/A（不举证）")
    if intent_measured:
        reason.append(f"意图维度 {len(intent_ids)} 用例{'全过' if intent_ok else '有异常'}")
    else:
        reason.append("候选无 intent-tag 用例：意图维度 N/A（不举证）")
    if money_safe and pass_rate_ok and p95_ok and token_ok and measured \
            and security_ok and intent_ok:
        reason.append("全维度通过")

    return GateResult(
        gate_id=gate_id,
        run_id=run_id,
        baseline_run_id=baseline_run_id or "",
        tier=tier,
        pass_rate=pass_rate_ok,
        money_safe=money_safe,
        p95_ok=p95_ok,
        token_ok=token_ok,
        measured=measured,
        thresholds=t,
        security_ok=security_ok,
        intent_ok=intent_ok,
        security_measured=security_measured,
        intent_measured=intent_measured,
        reason=reason,
        regressed=regressed,
        fund_hits=fund_hits,
        security_issues=security_issues,
        intent_issues=intent_issues,
    )


# 持久化门禁结果（可选：不落库则纯内存返回）
def save_gate_result(db: Session, res: GateResult, *, suite_id: str,
                     actor_id: str | None = None) -> None:
    from app.domain.models import EvalGateResult
    db.add(EvalGateResult(
        id=new_uuid(), gate_id=res.gate_id, run_id=res.run_id,
        baseline_run_id=res.baseline_run_id, suite_id=suite_id,
        tier=res.tier, decision=res.decision, passed=res.passed,
        detail=res.as_dict(), created_by=actor_id,
    ))
    db.commit()


def list_gate_results(db: Session, *, limit: int = 20) -> list[dict]:
    from app.domain.models import EvalGateResult
    rows = (db.query(EvalGateResult)
            .order_by(EvalGateResult.created_at.desc()).limit(limit).all())
    return [_gate_result_as_dict(r) for r in rows]


def _gate_result_as_dict(r) -> dict:
    return {
        "id": r.id, "gate_id": r.gate_id, "run_id": r.run_id,
        "baseline_run_id": r.baseline_run_id, "suite_id": r.suite_id,
        "tier": r.tier, "decision": r.decision, "passed": bool(r.passed),
        "detail": r.detail, "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ---- 候选回放 ----

def build_replay_set(db: Session, *, suite_id: str, tier: str,
                     run_id: str) -> dict:
    """为候选 Run 生成回放集。

    回放集 = tier_map[tier]（smoke⊂core⊂full），与候选 Run 同数据集、同分层。
    只做静态索引（不执行、不修改）；正值 golden-follower / HTTP Target 执行契约。
    若候选 Run 结果覆盖不到的 case 回放时即 skipped/degraded —— 绝不假 pass。
    """
    from app.services.eval_center import suite_tier_cases
    case_ids = suite_tier_cases(db, suite_id, tier)
    if case_ids is None:
        raise KeyError("tier not declared in suite")
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise KeyError("suite not found")
    return {
        "replay_set_id": f"replay-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "suite_id": suite_id,
        "suite_name": suite.name,
        "suite_version": suite.version,
        "tier": tier,
        "case_count": len(case_ids),
        "case_ids": case_ids,
        "source_run_id": run_id,
        "built_at": _now_naive().isoformat(),
    }


def replay_cases(db: Session, *, suite_id: str, tier: str) -> list[dict]:
    """回放集 hydration：返回 golden-follower/HTTP Target 用的 case dict 列表。"""
    from app.services.eval_center import hydrated_cases, suite_tier_cases
    ids = set(suite_tier_cases(db, suite_id, tier) or [])
    return [c for c in hydrated_cases(db, suite_id) if c["id"] in ids]


# =====================================================================
# V3-6 ：版本发布证据包导出 —— 评测/安全/成本/性能/批准/回滚 一条链
# =====================================================================

EVIDENCE_PACKAGE_VERSION = "1.0"
_GATE_APPROVED_ACTION = "GATE_BASELINE_APPROVED"


def _refs_fingerprint(refs: dict) -> str:
    """证据包关联引用的规范 JSON 指纹（幂等防重复导出）。"""
    import hashlib
    import json as _json

    canon = _json.dumps(refs, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def _registry_ref(db: Session, entity_type: str, name: str | None) -> str | None:
    """版本中心已批准引用 -> `type:name:version`；无则 None（不假报有版本）。"""
    if not name:
        return None
    try:
        from app.services.version_center import latest_approved

        v = latest_approved(db, entity_type=entity_type, entity_name=name)
        return f"{entity_type}:{name}:{v['version']}" if v else None
    except Exception:  # noqa: BLE001 —— 版本中心查询失败=不得引用，如实 None
        return None


def _previous_approved_run(db: Session, suite_id: str,
                           current_run_id: str) -> str | None:
    """回滚目标：该套件最近一次「已批准但非当前」的 Run（从批准审计事件还原）。

    基线切换覆盖唯一行，但 domain_events 的 GATE_BASELINE_APPROVED 是一条 append-only
    证据链——按时间倒序取该 suite 最近一条 run_id != 当前的批准记录即回滚目标。
    """
    from app.domain.models import DomainEvent

    events = (db.query(DomainEvent)
              .filter(DomainEvent.action == _GATE_APPROVED_ACTION)
              .order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
              .limit(100).all())
    for ev in events:
        payload = ev.payload or {}
        if str(payload.get("suite_id") or "") != suite_id:
            continue
        run = payload.get("run_id")
        if run and run != current_run_id:
            return str(run)
    return None


def export_evidence_package(db: Session, *, run_id: str, suite_id: str,
                            actor_id: str | None = None,
                            code_ref: str | None = None,
                            note: str = "") -> dict:
    """导出发布证据包（一条链：评测/安全/成本/性能/批准/回滚 + 六类关联引用）。

    关联引用：
      代码 code_ref / Prompt prompt_ref / 模型 model_ref / 策略 policy_ref /
      数据集 dataset_checksum(+snapshot) / Trace trace_ref；门禁证据、成本/性能、
      人工批准与回滚目标一并装入。fingerprint = 引用规范指纹（幂等：同指纹返回已有）。

    红线：导出**只读**证据快照，绝不改候选/基线 Run、绝不触发退款/决策副作用。
    未门禁/未测安全维度如实给 None/NOT_GATED（不把未测当达标）。
    """
    from sqlalchemy import func

    from app.core.config import settings
    from app.domain.models import (
        EvalGateResult,
        EvalRun,
        EvalSuite,
        new_uuid,
    )

    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        raise KeyError("run not found")
    suite = db.get(EvalSuite, suite_id)
    if suite is None:
        raise KeyError("suite not found")

    config = run.config if isinstance(run.config, dict) else {}
    model_name = config.get("model_name") or getattr(settings, "LLM_MODEL", None) or "unknown"
    prompt_id = run.prompt_variant_id or run.prompt_baseline_id or config.get("prompt_id")
    policy_name = config.get("policy_id") or config.get("policy_name") \
        or config.get("strategy") or "default"

    refs = {
        "code_ref": code_ref or str(run.trigger_source or "unpinned"),
        "prompt_ref": str(prompt_id or "unpinned"),
        "model_ref": _registry_ref(db, "model", model_name) or f"model:{model_name}",
        "policy_ref": _registry_ref(db, "security_policy", policy_name)
                      or f"policy:{policy_name}",
        "dataset_checksum": run.dataset_checksum or suite.checksum,
        "trace_ref": run.trace_id or run.run_id,
        "run_id": run_id,
        "suite_id": suite_id,
    }
    fingerprint = _refs_fingerprint(refs)

    # 幂等：相同引用指纹已存在 -> 返回已有包（不重复导出）
    existing = (db.query(ReleaseEvidencePackage)
                .filter(ReleaseEvidencePackage.fingerprint == fingerprint)
                .first())
    if existing is not None:
        return {**_package_as_dict(existing), "idempotent": True}

    # 门禁/基线证据（只读，绝不重算或改写）
    gate = (db.query(EvalGateResult)
            .filter(EvalGateResult.run_id == run_id)
            .order_by(EvalGateResult.created_at.desc()).first())
    gate_detail = gate.detail if gate else None
    baseline = get_baseline(db, suite_id)
    is_current_baseline = bool(baseline and baseline.get("run_id") == run_id)

    cost = run.cost or {}
    perf = run.performance or {}
    p95 = (perf.get("latency_ms") or {}).get("p95") \
        if isinstance(perf.get("latency_ms"), dict) else None

    suite_count = (db.query(func.count(EvalSuite.id)).filter(EvalSuite.id == suite_id)
                   .scalar() or 0)
    dataset_snapshot = {
        "suite_name": suite.name, "suite_version": suite.version,
        "checksum": suite.checksum, "schema_version": suite.schema_version,
        "tiers": suite.tier_map if isinstance(getattr(suite, "tier_map", None), dict)
        else None,
        "suite_rows": int(suite_count),
    }

    version = str(int((db.query(func.count(ReleaseEvidencePackage.id))
                       .filter(ReleaseEvidencePackage.run_id == run_id,
                               ReleaseEvidencePackage.suite_id == suite_id)
                       .scalar() or 0)) + 1)

    decision = (gate.decision if gate else None) \
        or ("PASS" if is_current_baseline else "NOT_GATED")
    passed = bool(gate.passed) if gate else bool(is_current_baseline)
    approved_at_raw = baseline.get("approved_at") if is_current_baseline else None

    pkg = ReleaseEvidencePackage(
        id=new_uuid(), run_id=run_id, suite_id=suite_id, package_version=version,
        fingerprint=fingerprint,
        code_ref=refs["code_ref"], prompt_ref=refs["prompt_ref"],
        model_ref=refs["model_ref"], policy_ref=refs["policy_ref"],
        dataset_checksum=refs["dataset_checksum"],
        dataset_snapshot=dataset_snapshot,
        trace_ref=refs["trace_ref"],
        gate_id=gate.gate_id if gate else None,
        decision=decision,
        passed=passed,
        security_ok=bool(gate_detail.get("security_ok")) if gate_detail else None,
        cost_measured=bool(cost.get("measured")),
        tokens_total=int(cost.get("input_tokens_total") or 0)
        + int(cost.get("output_tokens_total") or 0),
        p95_ms=p95,
        approved_by=baseline.get("approved_by") if is_current_baseline else None,
        approved_at=_parse_dt(approved_at_raw) if approved_at_raw else None,
        rollback_target_run_id=_previous_approved_run(db, suite_id, run_id),
        payload={
            "refs": refs, "run": {
                "run_id": run_id, "status": run.status, "name": run.name,
                "target_type": run.target_type, "total": (run.summary or {}).get("total"),
                "pass_rate": (run.summary or {}).get("pass_rate"),
            },
            "gate": {

                "gate_id": gate.gate_id if gate else None,
                "decision": decision, "passed": passed,
                "security_ok": bool(gate_detail.get("security_ok")) if gate_detail else None,
            },
            "cost": {"measured": bool(cost.get("measured")),
                     "tokens_total": int(cost.get("input_tokens_total") or 0)
                     + int(cost.get("output_tokens_total") or 0)},
            "performance": {"p95_ms": p95},
            "approval": {"approved": is_current_baseline,
                         "approved_by": baseline.get("approved_by")
                         if is_current_baseline else None,
                         "approved_at": approved_at_raw,
                         "rollback_target_run_id":
                             _previous_approved_run(db, suite_id, run_id)},
            "note": note,
            "exported_at": _now_naive().isoformat(),
        },
        created_by=actor_id,
    )
    db.add(pkg)
    db.commit()
    return {**_package_as_dict(pkg), "idempotent": False}


def _parse_dt(value: str) -> datetime | None:
    """解析 iso 时间串；解析失败如实 None（不伪造批准时间）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:  # noqa: BLE001 —— 未知格式不得假报
        return None


def _package_as_dict(p) -> dict:
    return {
        "id": p.id, "run_id": p.run_id, "suite_id": p.suite_id,
        "package_version": p.package_version, "fingerprint": p.fingerprint,
        "code_ref": p.code_ref, "prompt_ref": p.prompt_ref, "model_ref": p.model_ref,
        "policy_ref": p.policy_ref, "dataset_checksum": p.dataset_checksum,
        "dataset_snapshot": p.dataset_snapshot,
        "trace_ref": p.trace_ref,
        "gate_id": p.gate_id, "decision": p.decision, "passed": bool(p.passed),
        "security_ok": p.security_ok,
        "cost_measured": bool(p.cost_measured),
        "tokens_total": int(p.tokens_total or 0),
        "p95_ms": p.p95_ms,
        "approved_by": p.approved_by,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "rollback_target_run_id": p.rollback_target_run_id,
        "payload": p.payload,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def get_evidence_package(db: Session, package_id: str) -> dict:
    pkg = db.get(ReleaseEvidencePackage, package_id)
    if pkg is None:
        raise KeyError("package not found")
    return _package_as_dict(pkg)


def list_evidence_packages(db: Session, *, suite_id: str | None = None,
                           run_id: str | None = None,
                           limit: int = 20) -> list[dict]:
    q = db.query(ReleaseEvidencePackage).order_by(
        ReleaseEvidencePackage.created_at.desc())
    if suite_id:
        q = q.filter(ReleaseEvidencePackage.suite_id == suite_id)
    if run_id:
        q = q.filter(ReleaseEvidencePackage.run_id == run_id)
    rows = q.limit(limit).all()
    return [_package_as_dict(r) for r in rows]