# -*- coding: utf-8 -*-
"""V3-6 ：版本发布证据包 —— 评测/安全/成本/性能/批准/回滚 一条链完整关联。

覆盖：
- E1 六类关联引用齐全：代码 / Prompt / 模型 / 策略 / 数据集(checksum+snapshot) / Trace；
- E2 门禁证据关联：gate_id 指向真实门禁判定，decision/passed/security_ok 如实；
- E3 诚实未测量：未门禁 -> NOT_GATED + security_ok=None（绝不把未测当达标）；
- E4 幂等防重复：同引用指纹导出 -> 返回已有包（idempotent，不重复造链）；
- E5 版本升级：引用变更 -> package_version 递增（run×suite×version 唯一）；
- E6 回滚目标：从 GATE_BASELINE_APPROVED append-only 审计链还原最近一次已批准 Run；
- E7 API：POST evidence-export / GET list / GET detail；RBAC 需 LEADER。

红线：证据包导出只读（不改候选/基线 Run、不触发任何退款/决策副作用）；
未测得维度如实 None/NOT_GATED，绝不假报；错误/降级/未测一律不当通过。
"""
import uuid
from datetime import datetime, timezone

from app.domain import models
from app.services import eval_center as ec
from app.services import release_gate as rg
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login
from tests.test_release_gate import _seed_suite, _fake_baseline_run, _run_smoke_on


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gate_event(s, suite_id, run_id, actor="u-lead", created_at=None):
    s.add(models.DomainEvent(
        id=models.new_uuid(), case_id=None, actor_id=actor,
        action="GATE_BASELINE_APPROVED",
        payload={"suite_id": suite_id, "run_id": run_id, "note": "audit-chain"},
        created_at=created_at or _now(),
    ))
    s.commit()


# ---- E1 六类关联引用完整 ----

def test_export_links_all_six_refs():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _run_smoke_on(s, suite, name="E1")
        run_obj = s.query(models.EvalRun).filter_by(run_id=run["run_id"]).first()
        run_obj.prompt_variant_id = f"pt-{uuid.uuid4().hex[:8]}"
        run_obj.trigger_source = "manual"
        run_obj.config = {"model_name": "ev-model-x", "policy_id": "pol-a"}
        s.commit()

        pkg = rg.export_evidence_package(
            s, run_id=run["run_id"], suite_id=suite["id"],
            actor_id="u-lead", code_ref="abc123def", note="E1")

        assert pkg["code_ref"] == "abc123def"                     # 代码
        assert pkg["prompt_ref"] == run_obj.prompt_variant_id      # Prompt
        assert pkg["model_ref"].startswith("model:ev-model-x")     # 模型
        assert pkg["policy_ref"].startswith("policy:pol-a")        # 策略
        assert pkg["dataset_checksum"]                              # 数据集 checksum
        assert pkg["trace_ref"]                                     # Trace
        assert pkg["dataset_snapshot"]["suite_name"] == suite["name"]
        assert pkg["dataset_snapshot"]["checksum"] == suite["checksum"]
        assert pkg["dataset_snapshot"]["suite_rows"] == 1
        refs = pkg["payload"]["refs"]
        for key in ("code_ref", "prompt_ref", "model_ref", "policy_ref",
                    "dataset_checksum", "trace_ref", "run_id", "suite_id"):
            assert refs[key], key                            # 关联引用一个都不能缺
    finally:
        s.close()


# ---- E2 门禁证据关联 ----

def test_export_after_gate_links_gate_security_cost_perf():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        base_run = _fake_baseline_run(s, suite)
        rg.approve_baseline(s, run_id=base_run, suite_id=suite["id"],
                            actor_id="u-lead", note="base")
        cand = _fake_baseline_run(s, suite)
        res = rg.evaluate_release_gate(s, run_id=cand, suite_id=suite["id"],
                                       baseline_run_id=base_run, tier="smoke")
        assert res.passed is True and res.decision == "PASS"
        rg.save_gate_result(s, res, suite_id=suite["id"], actor_id="u-lead")

        pkg = rg.export_evidence_package(
            s, run_id=cand, suite_id=suite["id"], actor_id="u-lead",
            code_ref="gate-chain")

        assert pkg["gate_id"] == res.gate_id                 # 门禁判定证据关联
        assert pkg["decision"] == "PASS"
        assert pkg["passed"] is True
        assert pkg["security_ok"] is True
        assert pkg["cost_measured"] is True                  # 成本实测
        assert pkg["tokens_total"] == 1500
        assert pkg["p95_ms"] == 300                          # 性能 P95
        assert pkg["payload"]["gate"]["gate_id"] == res.gate_id
    finally:
        s.close()


# ---- E3 诚实未测量：未门禁 -> NOT_GATED + security_ok=None ----

def test_export_unmeasured_honest_not_gated():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _fake_baseline_run(s, suite)                   # 无门禁、非基线、成本未实测
        run_obj = s.query(models.EvalRun).filter_by(run_id=run).first()
        run_obj.cost = {"input_tokens_total": 0, "output_tokens_total": 0,
                        "cost_total": 0.0, "measured": False}
        run_obj.performance = {}                             # 无性能测量 -> p95=None（不假报）
        s.commit()

        pkg = rg.export_evidence_package(s, run_id=run, suite_id=suite["id"])
        assert pkg["decision"] == "NOT_GATED"                # 未门禁如实标注
        assert pkg["passed"] is False                        # 不开绿灯
        assert pkg["gate_id"] is None
        assert pkg["security_ok"] is None                    # 未测得安全维度 = None
        assert pkg["cost_measured"] is False                 # 未实测 -> False
        assert pkg["p95_ms"] is None
        assert pkg["tokens_total"] == 0
    finally:
        s.close()


# ---- E4 幂等防重复 ----

def test_export_idempotent_same_fingerprint():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _run_smoke_on(s, suite, name="E4")
        p1 = rg.export_evidence_package(s, run_id=run["run_id"],
                                         suite_id=suite["id"],
                                         code_ref="idem-ref")
        p2 = rg.export_evidence_package(s, run_id=run["run_id"],
                                         suite_id=suite["id"],
                                         code_ref="idem-ref")
        assert p2["idempotent"] is True and p2["id"] == p1["id"]
        assert p1["fingerprint"] == p2["fingerprint"]
        n = (s.query(models.ReleaseEvidencePackage)
             .filter_by(fingerprint=p1["fingerprint"]).count())
        assert n == 1                                        # 同引用只导出一份
    finally:
        s.close()


# ---- E5 引用变更 -> 版本升级 ----

def test_export_version_bump_on_changed_refs():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _run_smoke_on(s, suite, name="E5")
        p1 = rg.export_evidence_package(s, run_id=run["run_id"],
                                         suite_id=suite["id"], code_ref="ver-1")
        p2 = rg.export_evidence_package(s, run_id=run["run_id"],
                                         suite_id=suite["id"], code_ref="ver-2")
        assert p1["package_version"] == "1"
        assert p2["package_version"] == "2"
        assert p2["idempotent"] is False
        # run×suite×version 唯一：同 run/suite 导出两份不同版本合法，同指纹不重复
        assert p1["fingerprint"] != p2["fingerprint"]
    finally:
        s.close()


# ---- E6 回滚目标：从批准审计链还原最近一次已批准 Run ----

def test_export_rollback_target_from_approval_chain():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run1 = _fake_baseline_run(s, suite)
        _gate_event(s, suite["id"], run1, created_at=_now().replace(second=0))
        run2 = _fake_baseline_run(s, suite)
        _gate_event(s, suite["id"], run2, created_at=_now().replace(second=30))
        rg.approve_baseline(s, run_id=run2, suite_id=suite["id"],
                            actor_id="u-lead", note="current")

        pkg = rg.export_evidence_package(s, run_id=run2, suite_id=suite["id"],
                                         actor_id="u-lead")
        assert pkg["rollback_target_run_id"] == run1          # 最近一次已批准非当前 Run
        assert pkg["approved_by"] == "u-lead"
        assert pkg["payload"]["approval"]["rollback_target_run_id"] == run1
    finally:
        s.close()


def test_export_no_rollback_when_chain_empty():
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _run_smoke_on(s, suite, name="E6b")
        pkg = rg.export_evidence_package(s, run_id=run["run_id"],
                                         suite_id=suite["id"])
        assert pkg["rollback_target_run_id"] is None          # 无前序批准链 = 无可回滚
    finally:
        s.close()


# ---- E7 API 读写 + RBAC ----

def test_api_evidence_export_list_get(client):
    s = SessionTest()
    try:
        suite = ec.create_suite_from_golden(
            s, name=f"g-{uuid.uuid4().hex[:6]}", changelog="E7", owner_id="u")
        base_run = _fake_baseline_run(s, suite)
        h = _api_h(client, "leader1")
        r = client.post("/api/v1/evaluation-center/gate/baseline",
                        json={"suite_id": suite["id"], "run_id": base_run,
                              "note": "E7 base"}, headers=h)
        assert r.status_code == 201, r.text

        cand = _fake_baseline_run(s, suite)
        ev = client.post("/api/v1/evaluation-center/gate/evidence-export",
                         json={"suite_id": suite["id"], "run_id": cand,
                               "code_ref": "api-ref", "note": "E7"},
                         headers=h)
        assert ev.status_code == 201, ev.text
        item = ev.json()["item"]
        assert item["code_ref"] == "api-ref"
        assert item["fingerprint"] and item["package_version"] == "1"

        lst = client.get(
            f"/api/v1/evaluation-center/gate/evidence-packages?run_id={cand}",
            headers=h)
        assert lst.status_code == 200
        items = lst.json()["items"]
        assert items and items[0]["run_id"] == cand

        got = client.get(
            f"/api/v1/evaluation-center/gate/evidence-packages/{item['id']}",
            headers=h)
        assert got.status_code == 200
        assert got.json()["item"]["id"] == item["id"]
    finally:
        s.close()


def test_api_evidence_rbac_cs_denied(client):
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        run = _fake_baseline_run(s, suite)
        h = _api_h(client, "customer_service1")
        r = client.post("/api/v1/evaluation-center/gate/evidence-export",
                        json={"suite_id": suite["id"], "run_id": run},
                        headers=h)
        assert r.status_code == 403                          # CS 无权导出证据包
    finally:
        s.close()


def test_api_evidence_export_missing_run_404(client):
    s = SessionTest()
    try:
        suite = _seed_suite(s)
        h = _api_h(client, "leader1")
        r = client.post("/api/v1/evaluation-center/gate/evidence-export",
                        json={"suite_id": suite["id"], "run_id": "run-nope"},
                        headers=h)
        assert r.status_code == 404
    finally:
        s.close()