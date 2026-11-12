# -*- coding: utf-8 -*-
"""评测 Run 消费者：stream:eval_run（group:eval）。

每条消息 = 一次评测 Run（run_id 即基类锁的 case_id）。Worker 从 Suite 水合用例 ->
逐条执行（进程内确定性执行器 / 黑盒 HTTP Target）-> 增量落库 save_case_result ->
汇总 finish_run + 归档 V1 兼容 JSON 报告（M13 历史可读）。

隔离红线：
- 评测永不直接改 RefundCase 得出“通过”；候选产出只来自黑盒 Target 或
  GoldenFollowerExecutor（零网络、零资金副作用）。
- 预留能力用例如实 SKIPPED_UNSUPPORTED，绝不显示通过。
- 未实测成本保持 measured=False；异常/取消不伪造 summary。
- 幂等/并发：基类 lock:case（run_id）+ mark_run_started 仅在 PENDING 起点；
  已终态 Run 拒绝重复执行。

A08：config.concurrency>1 时用例并行执行（保序），
performance.parallel 报告速度/错误/资源；DB 落库留在主线程逐条独立会话，
不让 worker 线程触碰 SQLAlchemy session（防 Lock wait 与跨线程会话共享）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.domain.models import (
    EVAL_RUN_CANCELLED,
    EVAL_RUN_COMPLETED,
    EVAL_RUN_FAILED,
    PROMPT_APPROVED,
    EvalRun,
    PromptVersion,
)
from app.infrastructure.db import SessionLocal
from app.infrastructure.redis_client import STREAM_DLQ, STREAM_EVAL_RUN, redis_client
from app.services import eval_center as ec
from app.worker.base import RedisStreamConsumer
from eval_refund.executors import GoldenFollowerExecutor, PromptAwareExecutor
from eval_refund.runner import _p50p95, _run_cases, run_one_case, save_default_report

logger = logging.getLogger("worker.eval")

STATUS_SKIPPED = "skipped"


class EvalRunConsumer(RedisStreamConsumer):
    stream = STREAM_EVAL_RUN
    group = "group:eval"
    retries = 2
    reclaim_min_idle = 8000
    case_lock_ttl = 900          # run 级锁，长 hold

    def __init__(self, worker_id: str | None = None, *,
                 session_factory=None):
        super().__init__(worker_id)
        self._db_factory = session_factory or SessionLocal

    def _db(self):
        return self._db_factory()

    # ---- 主流程：单条消息 = 一个 Run ----

    def process(self, message_id: str, fields: dict) -> None:
        run_id = fields.get("case_id") or fields.get("run_id") or ""
        if not run_id:
            return
        try:
            config = json.loads(fields.get("config") or "{}") or {}
        except (TypeError, ValueError):
            config = {}
        limit = int(fields.get("limit") or config.get("limit") or 0)
        mode = config.get("mode") or "in_process"
        # A08：并发数（0/1=串行，>1 并行执行）。上限由 runner 的并行护栏约束。
        concurrency = int(config.get("concurrency") or 0)

        db = self._db()
        try:
            run = db.query(EvalRun).filter_by(run_id=run_id).first()
            if run is None:
                logger.warning("run %s 不存在，跳过", run_id)
                return
            if run.status != "PENDING":
                logger.info("run %s 已 %s，跳过重复执行", run_id, run.status)
                return
            if run.target_type == "intent_router" or run.task_type == "intent_eval":
                # 意图评测使用固定 120 条专项数据集，不应被
                # 退款 Suite 水合/tier_map 逻辑覆盖。评测仍由同一 Redis
                # Consumer 消费，但在此分流到专项 Runner。
                from app.services.intent_periodic_eval import execute_intent_eval_run
                try:
                    item = execute_intent_eval_run(db, run_id)
                    logger.info("intent run %s 完成: %s", run_id,
                                item.get("summary"))
                except Exception:  # noqa: BLE001 - 专项 Runner 已将 Run 标记 FAILED
                    logger.exception("intent run %s failed", run_id)
                return
            # V2.0 触发来源（代码/Prompt/模型/策略/定时）：未落时按 stream 幂等补写，
            # 保证触发回归 Run 的报告/列表可追溯来源。
            source = (fields.get("trigger_source") or run.trigger_source or "manual")
            if not run.trigger_source and fields.get("trigger_source"):
                run.trigger_source = str(fields["trigger_source"])[:20]
                db.commit()
            ec.mark_run_started(db, run_id)
            suite_id = fields.get("suite_id") or run.suite_id or ""
            cases = ec.hydrated_cases(db, suite_id) if suite_id else []
            # M14 A-B：优先取 Run 绑定的 prompt 变体/基线；候选须已人工批准
            prompt_doc = self._active_prompt(db, run)
        finally:
            db.close()

        if not cases:
            from eval_refund.golden import GOLDEN_CASES
            cases = list(GOLDEN_CASES)
        # M18 分级：config.tier 优先按 suite tier_map 过滤（smoke/core/full）
        tier = config.get("tier")
        if tier:
            wanted = ec.suite_tier_cases(self._db(), suite_id, tier)
            if wanted is None:
                # 数据集未声明该分层 -> 明确失败而非静默全量跑
                ec.finish_run(self._db(), run_id, status=EVAL_RUN_FAILED,
                              error=f"数据集未声明分层 {tier}")
                logger.info("run %s 数据集无分层 %s", run_id, tier)
                return
            case_by_id = {c.get("id", ""): c for c in cases}
            cases = [case_by_id[i] for i in wanted if i in case_by_id]
        if limit:
            cases = cases[:limit]
        if not cases:
            ec.finish_run(self._db(), run_id, status=EVAL_RUN_FAILED,
                          error="分层过滤后无用例可执行")
            logger.info("run %s 分层过滤后无用例", run_id)
            return

        target = None
        if prompt_doc is not None:
            # M14 A-B：业务输出同 oracle，token/成本按真实组合 prompt 估算
            executor = PromptAwareExecutor(
                prompt=prompt_doc["prompt"],
                model_name=prompt_doc.get("model_name"),
                input_fallback=int(prompt_doc.get("input_fallback") or 0),
            )
        elif mode == "http":
            from eval_refund.target import RefundSystemHttpTarget
            target = RefundSystemHttpTarget()
            executor = None
        else:
            # V1.0 修复：能力验证 Suite（capabilities_suite）走真实 backend 能力
            # 执行器（非 oracle 对拍）。识别依据权威化：用例自带 capability 判
            # 别字段（golden 预留用例只有 tags/skip_if_unsupported，无 capability
            # —— 二者永不混用）。不依赖 suite 元数据形状，避免类型歧义。
            capability_executor = None
            from eval_refund.capabilities_executor import run_capability_case
            if any(c.get("capability") for c in cases):
                capability_executor = run_capability_case
            executor = capability_executor or GoldenFollowerExecutor()

        results: list[dict] = []
        parallel_metrics: dict | None = None
        try:
            if concurrency > 1:
                # A03/A08：并发执行（保序+失败隔离），串行基线在 runner 内补测。
                # 取消检查只做一次（进入前）：并发中途取消由下游 _cancelled 语义
                # 覆盖——runner 执行期间取消信号会推迟到落库阶段检查。
                if self._cancelled(run_id):
                    ec.finish_run(self._db(), run_id, status=EVAL_RUN_CANCELLED)
                    logger.info("run %s 已取消", run_id)
                    return
                # 预留能力用例（skip_if_unsupported）无副作用：串行处理为 SKIPPED，
                # 不与真实执行混合，保证 skipped 结构共享 _run_case 语义。
                # 注意：能力验证套件（capabilities_suite）中"带 capability 字段且能力
                # 已实现"的用例必须真实执行，不得误拆进 SKIPPED 通道（_real_executable）。
                skip_cases = [c for c in cases
                              if c.get("skip_if_unsupported")
                              and not self._real_executable(c)]
                exec_cases = [c for c in cases if c not in skip_cases]
                skipped = [self._run_case(c, run_id, executor, target) for c in skip_cases]
                if exec_cases:
                    parallel_metrics, exec_results = _run_cases(
                        exec_cases, target=target, executor=executor, run_id=run_id,
                        use_llm=False, llm_cb=None, parallel_workers=concurrency)
                else:
                    exec_results = []
                # 按原始顺序合并（skipped 在前/exec 按 Keep 各自顺序，用索引映射）
                results = [None] * len(cases)
                idx = 0
                si = 0
                ei = 0
                for case in cases:
                    if case.get("skip_if_unsupported") \
                            and not self._real_executable(case):
                        results[idx] = skipped[si]; si += 1
                    else:
                        results[idx] = exec_results[ei]; ei += 1
                    idx += 1
            else:
                # 串行（默认/兼容）：每条执行完立即落库，取消可在任意点生效
                for case in cases:
                    if self._cancelled(run_id):
                        ec.finish_run(self._db(), run_id, status=EVAL_RUN_CANCELLED)
                        logger.info("run %s 已取消", run_id)
                        return
                    res = self._run_case(case, run_id, executor, target)
                    results.append(res)
                    db2 = self._db()
                    try:
                        ec.save_case_result(db2, run_id, res,
                                            candidate=res.get("candidate"),
                                            llm_judge=(res.get("judge") or {}).get("llm"))
                    finally:
                        db2.close()

            # 落库（主线程，逐条独立会话——并发执行结果的入库不触碰 worker 线程）
            if concurrency > 1:
                for res in results:
                    db2 = self._db()
                    try:
                        ec.save_case_result(db2, run_id, res,
                                            candidate=res.get("candidate"),
                                            llm_judge=(res.get("judge") or {}).get("llm"))
                    finally:
                        db2.close()
            summary = _summary(results)
            perf = _perf(results)
            if parallel_metrics is not None:
                # A08：并行度量并入 performance.parallel
                perf = dict(perf)
                perf["parallel"] = parallel_metrics
            cost = _cost(results)
            path = _archive(run_id, suite_id, cases, results, summary, perf, cost,
                            limit, trigger_source=source)
            ec.finish_run(self._db(), run_id, status=EVAL_RUN_COMPLETED,
                          summary=summary, performance=perf, cost=cost)
            self._record_report_path(run_id, path)
            # ：Run 完成后自动漂移监测 + 成本预算检查（只观测，绝不改决策）
            self._post_finish_observability(run_id, results, perf, cost)
            logger.info("run %s 完成: %s", run_id, summary)
        except Exception as exc:  # noqa: BLE001 —— 整 run 失败标记 FAILED
            logger.exception("run %s failed", run_id)
            ec.finish_run(self._db(), run_id, status=EVAL_RUN_FAILED,
                          error=str(exc)[:500])

    # ---- 单条用例 ----

    def _run_case(self, case: dict, run_id: str, executor, target) -> dict:
        skip = case.get("skip_if_unsupported")
        if skip:
            # 能力验证套件用例（带 capability 字段）且能力已实现 -> 真实执行；
            # 其余（golden 预留用例 / 能力未实现）-> 诚实 SKIPPED，
            # 真实执行失败一律 degraded——绝不伪 pass（契约）。
            if self._real_executable(case):
                try:
                    return run_one_case(case, target=None, executor=executor,
                                        run_id=run_id)
                except Exception as exc:  # noqa: BLE001 —— 能力执行异常 -> degraded
                    logger.warning("capability case %s exec failed: %s",
                                   case.get("id"), exc)
                    return {
                        "case_id": case.get("id") or case.get("case_id") or "",
                        "case_type": case.get("case_type", ""),
                        "schema_version": "1.0",
                        "execution": {"mode": "capability", "latency_ms": 0.0,
                                      "polling_rounds": None},
                        "judge": {
                            "verdict": "degraded", "score": None,
                            "dimensions": {}, "evidence": [],
                            "judge_version": "1.0",
                            "failure_reason": f"capability {skip} 执行失败: {exc}",
                            "degraded_reason": str(exc), "llm": None,
                        },
                        "candidate": None, "degraded": True, "error": str(exc),
                    }
            return {
                "case_id": case.get("id") or case.get("case_id") or "",
                "case_type": case.get("case_type", ""),
                "schema_version": "1.0",
                "execution": {"mode": "skipped", "latency_ms": 0.0, "polling_rounds": None},
                "judge": {
                    "verdict": STATUS_SKIPPED, "score": None,
                    "dimensions": {}, "evidence": [],
                    "judge_version": "1.0",
                    "failure_reason": f"unsupported capability: {skip}",
                    "degraded_reason": f"未实现能力 {skip}", "llm": None,
                },
                "candidate": None, "degraded": False, "error": None,
            }
        return run_one_case(case, target=target, executor=executor, run_id=run_id)

    def _real_executable(self, case: dict) -> bool:
        """能力套件用例（capability 字段 + skip_if_unsupported）且能力已实现 -> 可真执行。

        golden 预留用例（只有 skip_if_unsupported/tags，无 capability 字段）恒不可执行
        ——插件安装后也不得把 golden 预留用例翻身成已实现通过（冻结红线）。
        """
        skip = case.get("skip_if_unsupported")
        return bool(case.get("capability")) and bool(skip) \
            and self._capability_available(skip)

    def _capability_available(self, cap: str) -> bool:
        """能力已实现可用？（读取 capability_plugin 注册表；失败默认不可用，不伪 pass）"""
        try:
            from eval_refund.capabilities import capability_table
            return (capability_table().get(cap) or {}).get("implemented") is True
        except Exception:  # noqa: BLE001
            return False

    def _post_finish_observability(self, run_id: str, results: list[dict],
                                   perf: dict, cost: dict) -> None:
        """Run 完成后的观测钩子：漂移记录 + 预算超限告警。

        预算维度：按 run_id / suite / model 分别 check_budget（若配置则超限告警）。
        遵循 observability 红线：只观测、仅写 EvalAlert，不触碰退款/评测决策。
        """
        try:
            from app.services import observability as obs
            db0 = self._db()
            try:
                # 漂移：把结果喂给 record_drift（只观测）
                obs.record_drift(db0, run_id=run_id, suite_id=None,
                                 results=results, perf=perf)
                # 成本预算：run 级 + 各 model 级真实 cost
                cost_total = float((cost or {}).get("cost_total") or 0.0)
                tokens = int((cost or {}).get("input_tokens_total") or 0) + \
                         int((cost or {}).get("output_tokens_total") or 0)
                obs.check_budget(db0, dimension="run", dim_key=run_id,
                                 period="per_run", amount=cost_total, tokens=tokens)
                for model_key, mc in ((cost or {}).get("by_model") or {}).items():
                    obs.check_budget(db0, dimension="model", dim_key=model_key,
                                     period="per_run", amount=float(mc.get("cost") or 0),
                                     tokens=int(mc.get("tokens") or 0))
                db0.commit()
            finally:
                db0.close()
        except Exception:  # noqa: BLE001 —— 观测失败绝不影响 Run 完成状态
            logger.exception("post-finish observability failed for run %s", run_id)

    def _active_prompt(self, db, run: EvalRun) -> dict | None:
        """解析 Run 绑定的 prompt（A-B 变体/基线）为执行器配置。

        只允许 APPROVED 的 PromptVersion 进入执行（人工批准门）；
        未绑定或未批准返回 None（走普通 in_process / http）。
        """
        pid = run.prompt_variant_id or run.prompt_baseline_id or ""
        if not pid:
            return None
        pv = db.get(PromptVersion, pid)
        if pv is None or pv.status != PROMPT_APPROVED:
            return None
        return {
            "mode": "prompt_ab",
            "prompt": pv.content,
            "model_name": pv.model_name,
        }

    # ---- 取消 / 结果落盘 ----

    def _cancelled(self, run_id: str) -> bool:
        db = self._db()
        try:
            r = db.query(EvalRun).filter_by(run_id=run_id).first()
            return bool(r and r.status == EVAL_RUN_CANCELLED)
        finally:
            db.close()

    def _record_report_path(self, run_id: str, path: str) -> None:
        db = self._db()
        try:
            r = db.query(EvalRun).filter_by(run_id=run_id).first()
            if r is not None:
                r.report_path = path
                db.commit()
        finally:
            db.close()

    # 评测不触碰业务案件
    def _mark_system_failure(self, case_id: str) -> None:
        return

    def _dlq(self, message_id: str, fields: dict, error: Exception) -> None:
        logger.error("eval DLQ run=%s err=%s", fields.get("case_id", ""), error)
        redis_client.xadd(STREAM_DLQ, {
            "stream": self.stream, "group": self.group, "mid": message_id,
            "case_id": fields.get("case_id", ""), "error": str(error)[:500],
            "worker": self.worker_id,
        })
        self._ack(message_id)
        ec.finish_run(self._db(), fields.get("case_id", ""),
                      status=EVAL_RUN_FAILED, error=str(error)[:500])


# ---- 汇总辅助 ----

def _summary(results: list[dict]) -> dict:
    verdicts = [(r.get("judge") or {}).get("verdict") for r in results]
    n = len(results)
    passed = verdicts.count("pass")
    failed = verdicts.count("fail")
    skipped = verdicts.count(STATUS_SKIPPED)
    degraded = sum(
        1 for r in results
        if r.get("degraded")
        and (r.get("judge") or {}).get("verdict") not in ("pass", "fail", STATUS_SKIPPED)
    )
    return {
        "total": n, "passed": passed, "failed": failed,
        "degraded": degraded, "skipped_unsupported": skipped,
        "pass_rate": round(passed / n, 4) if n else 0.0,
    }


def _perf(results: list[dict]) -> dict:
    lat = [
        r["execution"].get("latency_ms")
        for r in results
        if r.get("execution") and r["execution"].get("latency_ms") is not None
    ]
    return {"latency_ms": _p50p95(lat or [])}


def _cost(results: list[dict]) -> dict:
    """汇总候选 usage：A-B（PromptAwareExecutor）打印真 measured token/成本。

     成本分账：
    - by_agent：按 agent（business / judge）汇总 token/成本+实测标记；
    - by_model：按 model 汇总（去重保留费率快照）；
    - by_source：{business, judge} 二元归属（LLM judge 未捕获 usage 时 judge=0/measured=false，
      如实不伪造）；
    - by_case：逐用例 token 明细（case_id -> 分账 reading）。
    未测（oracle / http 未回 usage）一律 measured=False，绝不伪造。
    """
    tin = tout = 0
    cost = 0.0
    measured = []
    cases = 0
    by_agent: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_source = {"business": {"cost": 0.0, "tokens": 0, "measured": False},
                 "judge": {"cost": 0.0, "tokens": 0, "measured": False}}
    by_case: dict[str, dict] = {}
    rates_of: dict[str, dict] = {}
    for r in results:
        cand = r.get("candidate") or {}
        usage = cand.get("usage") or {}
        cid = r.get("case_id", "")
        if not usage.get("measured"):
            continue
        agent = str(usage.get("agent") or "business")
        model = str(usage.get("model") or "")
        it = int(usage.get("input_tokens") or 0)
        ot = int(usage.get("output_tokens") or 0)
        c = float(usage.get("cost") or 0.0)
        tin += it
        tout += ot
        cost += c
        measured.append(cid)
        cases += 1

        # ：LLM judge 真实 usage（judge.llm.usage）并入 judge agent 分账。
        # 未捕获（None / 未配置）则 judge 源保持 measured=False——如实不伪造。
        judge_usage = ((r.get("judge") or {}).get("llm") or {}).get("usage")
        if judge_usage:
            jit = int(judge_usage.get("input_tokens") or 0)
            jot = int(judge_usage.get("output_tokens") or 0)
            if jit or jot:
                measured.append(f"judge:{cid}")
                jcost = 0.0  # 未配置费率：tokens 如实记，金额不伪造（费率未知）
                ba = by_agent.setdefault("judge", {"cost": 0.0, "tokens": 0,
                                                   "input_tokens": 0, "output_tokens": 0,
                                                   "measured_cases": 0, "measured": False})
                ba["tokens"] += jit + jot
                ba["input_tokens"] += jit
                ba["output_tokens"] += jot
                ba["measured_cases"] += 1
                ba["measured"] = True
                src = by_source["judge"]
                src["tokens"] += jit + jot
                src["measured"] = True
                by_case.setdefault(cid, {})["judge_tokens"] = jit + jot

        ba = by_agent.setdefault(agent, {"cost": 0.0, "tokens": 0,
                                         "input_tokens": 0, "output_tokens": 0,
                                         "measured_cases": 0, "measured": False})
        ba["cost"] = round(ba["cost"] + c, 6)
        ba["tokens"] += it + ot
        ba["input_tokens"] += it
        ba["output_tokens"] += ot
        ba["measured_cases"] += 1
        ba["measured"] = True

        if model:
            bm = by_model.setdefault(model, {"cost": 0.0, "tokens": 0,
                                             "measured_cases": 0, "rates": {}})
            bm["cost"] = round(bm["cost"] + c, 6)
            bm["tokens"] += it + ot
            bm["measured_cases"] += 1
            if usage.get("model_rates"):
                rates_of[model] = usage["model_rates"]

        src = by_source.get(agent) or by_source.setdefault(
            agent, {"cost": 0.0, "tokens": 0, "measured": False})
        src["cost"] = round(src["cost"] + c, 6)
        src["tokens"] += it + ot
        src["measured"] = True

        by_case[cid] = {
            "agent": agent, "model": model or None,
            "input_tokens": it, "output_tokens": ot,
            "cost": round(c, 6),
        }
    for m, d in by_model.items():
        d["rates"] = rates_of.get(m) or {}
    measured_all = len(results) > 0 and cases == len(results)
    return {
        "measured": bool(measured),
        "measured_cases": cases,
        "input_tokens_total": tin,
        "output_tokens_total": tout,
        "cost_total": round(cost, 6),
        "basis": "estimate_from_composed_prompt" if measured else None,
        "all_cases_measured": measured_all,
        # ---- 成本分账 ----
        "by_agent": by_agent,
        "by_model": by_model,
        "by_source": by_source,
        "by_case": by_case,
    }


def _capabilities_doc() -> dict:
    """能力声明：核心能力可用；四能力（security/intent/voice/coaching）如实反映
    capability_plugin 的实际注册状态（默认 unavailable，安装后 available）。

    永不伪造：未安装插件时 4 能力仍标 unavailable（M21 契约诚实性）。
    """
    from eval_refund.capabilities import capability_table
    tbl = capability_table()
    asserted_core = ["refund_workflow", "order_verification", "hitl"]
    return {
        "core": asserted_core + [k for k, v in tbl.items()
                                 if v.get("implemented")],
        "declared": {
            "security": {"availability": tbl.get("security", {}).get("availability", "unavailable"),
                         "implemented": tbl.get("security", {}).get("implemented", False)},
            "intent": {"availability": tbl.get("intent", {}).get("availability", "unavailable"),
                       "implemented": tbl.get("intent", {}).get("implemented", False)},
            "voice": {"availability": tbl.get("voice", {}).get("availability", "unavailable"),
                      "implemented": tbl.get("voice", {}).get("implemented", False),
                      "modality": "audio", "streaming": True},
            "coaching": {"availability": tbl.get("coaching", {}).get("availability", "unavailable"),
                         "implemented": tbl.get("coaching", {}).get("implemented", False)},
        },
        "target": {"audio": False, "streaming": False},
    }


def _archive(run_id: str, suite_id: str, cases: list[dict], results: list[dict],
             summary: dict, perf: dict, cost: dict, limit: int,
             trigger_source: str = "manual") -> str:
    from eval_refund.golden import GOLDEN_CASES, build_dataset_document
    doc = build_dataset_document()
    # extensions：实测能力证据（真实验证后不再恒 None——M21 契约：未实现仍 None，
    # 已实现由本运行回填该能力组聚合，绝不伪造；golden 恒 None 不变）。
    ext = {"security": None, "intent": None, "voice": None, "coaching": None}
    _feat_ext = ("security", "intent", "voice", "coaching")
    from eval_refund.capabilities import capability_table
    tbl = capability_table()
    # 实测能力证据：runner 结果 dict 的 candidate 是 out.as_dict()，extensions 嵌套其下
    rows = [r.get("candidate") or {} for r in results]
    for cap in _feat_ext:
        if not (tbl.get(cap) or {}).get("implemented"):
            continue
        ext_vals = [(r.get("extensions") or {}).get(cap) for r in rows]
        # 聚合该组所有真实证据（pass_condition / 直接指标），为报告断言的"已测量"
        ext[cap] = {
            "_aggregate": True,
            "measured_cases": sum(1 for v in ext_vals if v),
            "evidence": [v for v in ext_vals if v],
        }
    report = {
        "report_version": "1.0",
        "run_id": run_id,
        "trigger_source": trigger_source,   # V2.0 触发来源（代码/Prompt/模型/策略/manual/schedule）
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "dataset_version": doc["dataset_version"],
            "dataset_checksum": doc["checksum"],
            "suite_id": suite_id,
            "trigger_source": trigger_source,
            # M21 插件契约：能力声明 + Target 声明（只反映声明，未实现仍标不可用）
            "capabilities": _capabilities_doc(),
        },
        "dataset": {
            "version": doc["dataset_version"],
            "checksum": doc["checksum"],
            "case_count": len(cases),
            "full_count": len(GOLDEN_CASES),
            "limit": limit,
        },
        "summary": summary,
        "performance": perf,
        "cost": cost,
        "extensions": ext,
        "trace": {"trace_id": run_id, "run_id": run_id},
        "cases": results,
    }
    p = save_default_report(report)
    return str(p)
