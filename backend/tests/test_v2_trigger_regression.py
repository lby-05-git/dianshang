# -*- coding: utf-8 -*-
"""V2.0 -2 评测中心产品化增量契约测试（Phase 2）。

覆盖：
- S1 trigger_regression：code/prompt/model/policy 四来源落库 + 同一来源防抖 +
  未指定数据集自动选最新 ACTIVE；非法来源拒绝（schedule/manual 不由此入口触发）。
- S2 数据集分类：sync_suite_categories 幂等 upsert / category_distribution /
  list_suites(category=…) 筛选（与 refresh_dataset 同语义，add-only 不改 case）。
- S3 数据集版本对比：added/removed/changed + verdict（纯复制 identical；
  编辑复制 modified）。
- S4 Run 进度：done/total + 触发来源；定时调度到期自动触发（manual 绝不自动）。
- S5 失败告警：定时 Run FAILED → RUN_FAILED 告警（幂等）；列表/ack。
- S6 worker 溯源：从 stream 补写 trigger_source 到 Run + 归档报告（可追溯）。
- S7 API：回归触发 202 / 分类 / 对比 / 进度 / 告警 ack（均 leader 鉴权）。

红线：触发回归与定时评测只创建 PENDING Run 并入队，绝不自动改决策/退款。
"""
import json
import time
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s, name=None) -> dict:
    return ec.create_suite_from_golden(
        s, name=name or f"suite-{uuid.uuid4().hex[:8]}", changelog="V2-P2")


def _mk_schedule(s, suite, tier="smoke", cadence="manual", **kw):
    return ec.create_schedule(s, suite_id=suite["id"], tier=tier,
                              cadence=cadence, owner_id="u-test", **kw)


# ---- S1 trigger_regression ----

def test_trigger_regression_sources_all_four():
    """四变更来源各触发一次回归 Run：仅入队（PENDING），来源可追溯，绝不自动执行。"""
    s = SessionTest()
    suite = _seed_suite(s)
    seen = set()
    for source in ("code", "prompt", "model", "policy"):
        r = ec.trigger_regression(s, trigger_source=source, suite_id=suite["id"],
                                  created_by="u-test", reason=f"{source} 变更")
        assert r["dedup"] is False
        assert r["suite_id"] == suite["id"]
        assert r["trigger_source"] == source
        seen.add(r["run_id"])
        run = ec.get_run(s, r["run_id"])
        assert run["status"] == "PENDING"          # 只触发，不自动执行
        assert run["trigger_source"] == source
        assert run["config"]["regression"] is True
    assert len(seen) == 4                          # 不同来源各建一条 Run


def test_trigger_regression_dedup_while_pending():
    """同一来源 + 同一数据集存在 PENDING 回归 Run 时不重复入队（防抖）。"""
    s = SessionTest()
    suite = _seed_suite(s)
    r1 = ec.trigger_regression(s, trigger_source="code", suite_id=suite["id"],
                               created_by="u-test")
    r2 = ec.trigger_regression(s, trigger_source="code", suite_id=suite["id"],
                               created_by="u-test")
    assert r1["dedup"] is False and r2["dedup"] is True
    assert r1["run_id"] == r2["run_id"]
    # 换来源仍然放行
    r3 = ec.trigger_regression(s, trigger_source="prompt", suite_id=suite["id"],
                               created_by="u-test")
    assert r3["dedup"] is False and r3["run_id"] != r1["run_id"]


def test_trigger_regression_latest_active_suite():
    """未指定数据集时自动选最新 ACTIVE 数据集（回归跑当前基线）。

    created_at 秒级精度：连建两个 suite 且中间睡 1.1s，保证 newest 的秒级
    created_at 严格大于所有更早插入的 suite（杜绝与历史 suite 同秒并列）。
    """
    s = SessionTest()
    _seed_suite(s)                                  # 更旧的 ACTIVE
    time.sleep(1.1)
    newest = _seed_suite(s)                         # 严格最新的 ACTIVE
    r = ec.trigger_regression(s, trigger_source="policy", created_by="u-test",
                              reason="政策法规更新")
    assert r["suite_id"] == newest["id"]
    assert ec.get_run(s, r["run_id"])["config"]["regression"] is True


def test_trigger_regression_invalid_source_rejected():
    """非变更来源一概拒绝；指定不存在的数据集拒绝。"""
    s = SessionTest()
    suite = _seed_suite(s)
    for bad in ("manual", "schedule", "bogus"):
        with pytest.raises(ValueError, match="trigger_source"):
            ec.trigger_regression(s, trigger_source=bad, suite_id=suite["id"])
    with pytest.raises(KeyError):
        ec.trigger_regression(s, trigger_source="code", suite_id="suite-nope")


# ---- S2 数据集分类 ----

def test_dataset_category_sync_filter_and_distribution():
    s = SessionTest()
    suite = _seed_suite(s)
    r1 = ec.sync_suite_categories(s, suite["id"])
    assert r1["inserted"] == suite["case_count"]    # 全量用例可复算入图
    assert r1["skipped"] == 0
    r2 = ec.sync_suite_categories(s, suite["id"])
    assert r2["inserted"] == 0 and r2["skipped"] == suite["case_count"]  # 幂等
    dist = ec.category_distribution(s, suite["id"])
    assert dist["total"] == suite["case_count"]
    assert set(dist["categories"]) == {"core", "boundary", "anomaly",
                                       "concurrency", "security", "voice"}
    sec = dist["categories"]["security"]
    assert sec >= 1                                  # golden 含安全/意图预留用例
    # 按类别筛选：只显示该类别覆盖的用例数
    lst = ec.list_suites(s, category="security", size=100)
    mine = [i for i in lst["items"] if i["id"] == suite["id"]]
    assert mine and mine[0]["case_count"] == sec


# ---- S3 数据集版本对比 ----

def test_dataset_version_compare_modified():
    """复制编辑一处 -> changed 精确锁定该 case；基线零改动。"""
    s = SessionTest()
    base = _seed_suite(s)
    tgt = ec.copy_suite(
        s, base["id"], new_name=f"copy-{uuid.uuid4().hex[:8]}", changelog="v2",
        edits=[{"case_id": "G01", "fields": {"requested_amount": "999.99"}}])
    cmp = ec.compare_suite_versions(s, base["id"], tgt["id"])
    assert cmp["verdict"] == "modified"
    assert cmp["changed_cases"] == ["G01"]
    assert cmp["added_cases"] == [] and cmp["removed_cases"] == []
    assert cmp["base_cases"] == cmp["target_cases"] == base["case_count"]
    assert cmp["unchanged_cases"] == base["case_count"] - 1
    # 基线只读：原始 suite 的 G01 未被改动
    base_cases = {c["case_id"]: c for c in ec.get_suite_cases(s, base["id"])}
    assert base_cases["G01"]["inputs"]["requested_amount"] == "99.00"


def test_dataset_version_compare_pure_copy_identical():
    """纯复制（无编辑）-> 逐 case 指纹一致，verdict identical。"""
    s = SessionTest()
    base = _seed_suite(s)
    tgt = ec.copy_suite(s, base["id"],
                        new_name=f"pure-{uuid.uuid4().hex[:8]}",
                        changelog="v2", edits=[])
    cmp = ec.compare_suite_versions(s, base["id"], tgt["id"])
    assert cmp["verdict"] == "identical"
    assert cmp["changed_cases"] == [] and cmp["added_cases"] == []
    assert cmp["removed_cases"] == []
    assert cmp["unchanged_cases"] == base["case_count"]
    with pytest.raises(KeyError):
        ec.compare_suite_versions(s, "suite-nope", tgt["id"])


# ---- S4 运行进度 / 定时评测 ----

def test_run_progress_counts():
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}",
                        name="progress", suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test")
    p0 = ec.run_progress(s, run.run_id)
    assert p0["done_cases"] == 0 and p0["total_cases"] == suite["case_count"]
    assert p0["ratio"] == 0.0 and p0["trigger_source"] == "manual"
    ec.save_case_result(s, run.run_id,
                        {"case_id": "G01", "case_type": "auto_approve",
                         "judge": {"verdict": "PASS", "score": 1.0}},
                        candidate=None, llm_judge=None)
    ec.save_case_result(s, run.run_id,
                        {"case_id": "G02", "case_type": "high_value_approve",
                         "judge": {"verdict": "PASS", "score": 1.0}},
                        candidate=None, llm_judge=None)
    p2 = ec.run_progress(s, run.run_id)
    assert p2["done_cases"] == 2 and p2["total_cases"] == suite["case_count"]
    assert 0 < p2["ratio"] < 1.0
    with pytest.raises(KeyError):
        ec.run_progress(s, "run-nope")


def test_fire_due_schedules_auto_but_not_manual():
    """到期 daily 自动触发一次（last_run/next_due 落链），第二次不再重发；manual 绝不自动。"""
    s = SessionTest()
    suite = _seed_suite(s)
    sch = _mk_schedule(s, suite, tier="smoke", cadence="daily",
                       initial_delay_minutes=0)
    fired = ec.fire_due_schedules(s, limit=1000)
    mine = [f for f in fired["fired"] if f["schedule_id"] == sch["id"]]
    assert mine, "到期调度应被自动触发"
    assert mine[0]["cadence"] == "daily" and mine[0]["tier"] == "smoke"
    rid = mine[0]["run_id"]
    run = ec.get_run(s, rid)
    assert run["trigger_source"] == "schedule" and run["status"] == "PENDING"
    assert run["config"]["schedule_id"] == sch["id"]
    assert ec.get_schedule(s, sch["id"])["last_run_id"] == rid
    # 幂等：mark_schedule_fired 重排 next_due 后，第二次轮询不重发本调度
    fired2 = ec.fire_due_schedules(s, limit=1000)
    assert not any(f["schedule_id"] == sch["id"] for f in fired2["fired"])
    # manual 调度：哨兵绝不代跑
    suite2 = _seed_suite(s)
    schm = _mk_schedule(s, suite2, tier="smoke", cadence="manual")
    fired3 = ec.fire_due_schedules(s, limit=1000)
    assert not any(f["schedule_id"] == schm["id"] for f in fired3["fired"])
    assert ec.get_schedule(s, schm["id"])["last_run_id"] is None


# ---- S5 失败告警 ----

def test_failure_alert_generated_and_idempotent():
    s = SessionTest()
    suite = _seed_suite(s)
    sch = _mk_schedule(s, suite, tier="smoke", cadence="daily",
                       initial_delay_minutes=0)
    rid = next(f["run_id"] for f in ec.fire_due_schedules(s, limit=1000)["fired"]
               if f["schedule_id"] == sch["id"])
    ec.finish_run(s, rid, status=models.EVAL_RUN_FAILED, error="模拟执行失败")
    # 共享测试库只增不减：调度累积可能超默认 limit，显式放大窗口（同 due_schedules 教训）
    r1 = ec.raise_run_failure_alerts(s, limit=10000)
    assert rid in [a["run_id"] for a in r1["alerts"]]
    # 幂等：已有 RUN_FAILED 告警不再重复落
    r2 = ec.raise_run_failure_alerts(s, limit=10000)
    assert not any(a["run_id"] == rid for a in r2["alerts"])
    # 落库证据：alert_type/severity/run 关联
    with SessionTest() as check:
        alerts = (check.query(models.EvalAlert)
                  .filter_by(alert_type="RUN_FAILED", run_id=rid).all())
    assert len(alerts) == 1
    assert alerts[0].severity == "error"
    assert alerts[0].metric_key == f"schedule/{sch['cadence']}/{sch['tier']}:{sch['id'][:18]}"


def test_alert_list_and_ack_service():
    """告警列表/ack：run_id 唯一化，断言只针对本测试的告警（共享库只增不减）。"""
    s = SessionTest()
    suite = _seed_suite(s)
    rid = f"run-fail-{uuid.uuid4().hex[:8]}"       # 全局唯一，避免历史行干扰
    alert = models.EvalAlert(id=ec.new_uuid(), alert_type="RUN_FAILED",
                             run_id=rid, suite_id=suite["id"],
                             metric_key="schedule:t", message="boom",
                             severity="error")
    s.add(alert)
    s.commit()
    lst = ec.list_alerts(s, acked=False)
    mine = [i for i in lst["items"] if i["run_id"] == rid]
    assert mine and mine[0]["acked"] is False
    ack = ec.ack_alert(s, alert.id, acked_by="u-leader")
    assert ack["acked"] is True and ack["acked_by"] == "u-leader"
    assert not any(i["run_id"] == rid
                   for i in ec.list_alerts(s, acked=False)["items"])
    with pytest.raises(KeyError):
        ec.ack_alert(s, "alert-nope")


# ---- S6 worker 溯源 ----

def test_worker_stamps_trigger_source_from_stream():
    """迁移前存量 Run（trigger_source NULL）由 stream 补写 + 报告可追溯来源。"""
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}",
                        name="stamp", suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    run.trigger_source = None          # 模拟 V2.0 迁移前落库的 Run
    s.commit()
    fields = {
        "case_id": run.run_id, "run_id": run.run_id, "suite_id": suite["id"],
        "limit": "2", "config": json.dumps({"mode": "in_process"}),
        "trigger_source": "code",
    }
    EvalRunConsumer(session_factory=SessionTest).process("m18s1", fields)
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    assert got["trigger_source"] == "code"
    assert got["summary"]["total"] == 2
    assert got["report_path"]
    with open(got["report_path"], encoding="utf-8") as f:
        report = json.load(f)
    assert report["trigger_source"] == "code"
    assert report["environment"]["trigger_source"] == "code"


# ---- S7 API（leader 鉴权）----

def test_api_regression_trigger(client):
    s = SessionTest()
    suite = _seed_suite(s)
    h = _api_h(client, "leader1")
    r1 = client.post("/api/v1/evaluation-center/regressions/trigger",
                     json={"trigger_source": "code", "suite_id": suite["id"],
                           "reason": "重构评测服务"}, headers=h)
    assert r1.status_code == 202
    b1 = r1.json()
    assert b1["dedup"] is False and b1["trigger_source"] == "code"
    r2 = client.post("/api/v1/evaluation-center/regressions/trigger",
                     json={"trigger_source": "code", "suite_id": suite["id"]},
                     headers=h)
    assert r2.status_code == 202
    assert r2.json()["dedup"] is True and r2.json()["run_id"] == b1["run_id"]
    # 非变更来源被 schema 拒绝（本应用 schema 校验统一落 400）；指定数据集不存在则 404
    bad = client.post("/api/v1/evaluation-center/regressions/trigger",
                      json={"trigger_source": "schedule"}, headers=h)
    assert bad.status_code == 400
    miss = client.post("/api/v1/evaluation-center/regressions/trigger",
                       json={"trigger_source": "prompt",
                             "suite_id": "suite-nope"}, headers=h)
    assert miss.status_code == 404


def test_api_dataset_categories_and_filter(client):
    s = SessionTest()
    suite = _seed_suite(s)
    ec.sync_suite_categories(s, suite["id"])
    h = _api_h(client, "leader1")
    cats = client.get("/api/v1/evaluation-center/datasets/categories", headers=h)
    assert cats.status_code == 200
    body = cats.json()
    assert body["total"] >= 1
    assert set(body["categories"]) == {"core", "boundary", "anomaly",
                                       "concurrency", "security", "voice"}
    my_sec = ec.category_distribution(s, suite["id"])["categories"]["security"]
    flt = client.get("/api/v1/evaluation-center/datasets?category=security",
                     headers=h)
    assert flt.status_code == 200
    mine = [i for i in flt.json()["items"] if i["id"] == suite["id"]]
    assert mine and mine[0]["case_count"] == my_sec


def test_api_dataset_compare(client):
    s = SessionTest()
    base = _seed_suite(s)
    tgt = ec.copy_suite(s, base["id"],
                        new_name=f"copy-{uuid.uuid4().hex[:8]}", changelog="v2",
                        edits=[{"case_id": "G01",
                                "fields": {"requested_amount": "999.99"}}])
    h = _api_h(client, "leader1")
    r = client.get(f"/api/v1/evaluation-center/datasets/compare"
                   f"?base_suite_id={base['id']}&target_suite_id={tgt['id']}",
                   headers=h)
    assert r.status_code == 200
    b = r.json()
    assert b["verdict"] == "modified" and b["changed_cases"] == ["G01"]
    r404 = client.get(f"/api/v1/evaluation-center/datasets/compare"
                      f"?base_suite_id=suite-nope&target_suite_id={tgt['id']}",
                      headers=h)
    assert r404.status_code == 404


def test_api_run_progress(client):
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}",
                        name="progress", suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test")
    h = _api_h(client, "leader1")
    r = client.get(f"/api/v1/evaluation-center/runs/{run.run_id}/progress",
                   headers=h)
    assert r.status_code == 200
    b = r.json()
    assert b["run_id"] == run.run_id
    assert b["done_cases"] == 0 and b["total_cases"] == suite["case_count"]
    assert b["trigger_source"] == "manual"
    r404 = client.get("/api/v1/evaluation-center/runs/run-nope/progress",
                      headers=h)
    assert r404.status_code == 404


def test_api_alert_list_and_ack(client):
    s = SessionTest()
    suite = _seed_suite(s)
    rid = f"run-fail-{uuid.uuid4().hex[:8]}"       # 全局唯一，避免历史行干扰
    alert = models.EvalAlert(id=ec.new_uuid(), alert_type="RUN_FAILED",
                             run_id=rid, suite_id=suite["id"],
                             metric_key="schedule:t", message="boom",
                             severity="error")
    s.add(alert)
    s.commit()
    h = _api_h(client, "leader1")
    r = client.get("/api/v1/evaluation-center/schedules/alerts", headers=h)
    assert r.status_code == 200
    mine = [i for i in r.json()["items"] if i["run_id"] == rid]
    assert mine and mine[0]["acked"] is False
    ac = client.post(f"/api/v1/evaluation-center/schedules/alerts/{alert.id}/ack",
                     headers=h)
    assert ac.status_code == 200 and ac.json()["item"]["acked"] is True
    una = client.get("/api/v1/evaluation-center/schedules/alerts?acked=false",
                     headers=h)
    assert not any(i["run_id"] == rid for i in una.json()["items"])