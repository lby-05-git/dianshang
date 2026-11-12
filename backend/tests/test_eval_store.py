# -*- coding: utf-8 -*-
"""V2.0 评测持久化模型（M10）：Suite/Case/Run/Result/Artifact/PromptVersion 落库。

用户授权连续推进 V2.0 → V3.0，本文件随模块迭代增量补充测试。
"""
import hashlib
import uuid

from sqlalchemy.orm import Session

from app.domain import models
from tests.conftest import SessionTest


def _rnd(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _mk_suite(s: Session, version: str = "2.0.0") -> models.EvalSuite:
    snap = {"schema_version": "1.0", "dataset_version": version}
    su = models.EvalSuite(
        name=_rnd("suite"),
        version=version,
        schema_version="1.0",
        checksum=hashlib.sha256(b"x" * 16).hexdigest(),
        snapshot=snap,
        status=models.EVAL_SUITE_ACTIVE,
        owner_id="u-demo",
        changelog="M10 test",
    )
    s.add(su)
    s.commit()
    return su


def test_suite_and_case_roundtrip():
    s = SessionTest()
    su = _mk_suite(s)
    assert su.id
    c = models.EvalCase(
        suite_id=su.id,
        case_ref_id="G99",
        case_type="auto_approve",
        inputs={"order_id": "ORD-X1", "amount": "99.00"},
        expected={"current_status": "COMPLETED", "final_decision": "APPROVED"},
        constraints={"required_nodes": ["finalize"]},
    )
    s.add(c)
    s.commit()
    loaded = s.query(models.EvalCase).filter_by(suite_id=su.id, case_ref_id="G99").one()
    assert loaded.inputs["order_id"] == "ORD-X1"


def test_eval_run_full_cycle():
    s = SessionTest()
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    run = models.EvalRun(
        run_id=run_id,
        name="M10 run",
        task_type="refund_eval",
        priority=0,
        status=models.EVAL_RUN_RUNNING,
        suite_version="2.0.0",
        config={"limit": 3, "use_llm": False},
        summary={"total": 3, "passed": 3},
        performance={"p50": 12.5},
        cost={"measured": False},
    )
    s.add(run)
    s.commit()

    res = models.EvalCaseResult(
        run_id=run_id, case_ref_id="G01", verdict=models.EVAL_RESULT_PASS,
        score=1.0, dimensions=[{"name": "status", "ok": True, "measured": True}],
        degraded=False, candidate={"schema_version": "1.0"},
    )
    s.add(res)
    # 幂等：同 run 同 case 唯一（由创建方保证；这里验证不重复插入）
    s.commit()

    got = s.query(models.EvalRun).filter_by(run_id=run_id).one()
    assert got.status == models.EVAL_RUN_RUNNING
    assert got.summary["passed"] == 3
    results = s.query(models.EvalCaseResult).filter_by(run_id=run_id).all()
    assert len(results) == 1 and results[0].verdict == models.EVAL_RESULT_PASS


def test_artifact_and_prompt_version():
    s = SessionTest()
    art = models.EvalArtifact(
        kind="report", mime_type="application/vnd.ms-excel",
        checksum=hashlib.sha256(b"report").hexdigest(),
        size_bytes=100, storage_ref="reports/x.xlsx", sensitivity="internal",
    )
    s.add(art)
    pv = models.PromptVersion(
        name="sentiment-v1", agent_name="sentiment", version="1.0",
        content="你是一个舆情评分助手。",
        content_checksum=hashlib.sha256("prompt".encode()).hexdigest(),
        status=models.PROMPT_CANDIDATE, is_baseline=True,
    )
    s.add(pv)
    s.commit()
    # 候选版本默认不可自动替换生产：审批后才能 APPROVED
    assert pv.status == models.PROMPT_CANDIDATE
    assert s.get(models.EvalArtifact, art.id).mime_type.endswith("excel")


def test_eval_runs_unique_run_id():
    """run_id 唯一约束：重复 run_id 落库失败（幂等由创建层保证）。"""
    from sqlalchemy.exc import IntegrityError

    s = SessionTest()
    run_id = f"run-unq-{uuid.uuid4().hex[:10]}"
    run = models.EvalRun(run_id=run_id, task_type="refund_eval",
                         schema_version="1.0", priority=0,
                         status=models.EVAL_RUN_PENDING)
    s.add(run)
    s.commit()
    dup = models.EvalRun(run_id=run_id, task_type="refund_eval",
                         schema_version="1.0", priority=0,
                         status=models.EVAL_RUN_PENDING)
    s.add(dup)
    try:
        s.commit()
        raise AssertionError("重复 run_id 不应落库")
    except IntegrityError:
        s.rollback()