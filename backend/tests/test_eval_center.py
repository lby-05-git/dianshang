# -*- coding: utf-8 -*-
"""M11 评测中心数据集版本管理（service + API）契约测试。

覆盖：
- service：create_suite_from_golden / list_suites / get_suite / get_suite_cases /
  copy_suite（改版靠复制，基线只读，edits 合入，红线校验）。
- API（/api/v1/evaluation-center/datasets）：创建 / 列表 / 详情 / 用例 / 复制 / 删除，
  角色门禁（supervisor+ 只读及一键评测，leader+ 治理，admin 删除）。
- 红线 ：非 APPROVED 不得 SUCCESS 退款；预留能力标签不得伪装通过；
  被 Run 引用的数据集不可删除。
"""
import hashlib
import uuid

import pytest

from sqlalchemy.orm import Session

from app.api import routes_eval_center as eval_routes
from app.domain import models
from app.services import eval_center as ec
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _rnd(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _mk_suite_obj(version: str = "2.0.0") -> models.EvalSuite:
    snap = {"schema_version": "1.0", "dataset_version": version}
    return models.EvalSuite(
        name=_rnd("suite"),
        version=version,
        schema_version="1.0",
        checksum=hashlib.sha256(f"k-{version}".encode()).hexdigest(),
        snapshot=snap, status=models.EVAL_SUITE_ACTIVE,
        owner_id="u-demo", changelog="M11 test",
    )


def _seed_suite(s: Session) -> models.EvalSuite:
    su = _mk_suite_obj()
    s.add(su)
    s.commit()
    return su


def _case_doc(case_id: str, *, decision="APPROVED", refund="SUCCESS",
              status="COMPLETED", tags=None) -> dict:
    return {
        "id": case_id, "order_id": _rnd("ORD"), "requested_amount": "150.00",
        "reason": "质量问题", "channel": "app", "case_type": "auto_approve",
        "expected": {"current_status": status, "final_decision": decision,
                     "refund_status": refund},
        "allow": True, "max_wait_seconds": 30,
        "required_nodes": ["finalize"], "forbidden_nodes": [],
        "side_effect": "" if refund == "SUCCESS" else "no_refund",
        "tags": tags or [], "skip_if_unsupported": None,
    }


def _greedy_doc() -> dict:
    return {
        "schema_version": "1.0", "dataset_version": "2.0.0",
        "checksum": "seed", "modalities": ["text"], "required_capabilities": ["refund_workflow"],
        "reserved_tags": ["voice", "intent"], "cases": [
            _case_doc("G01"), _case_doc("G02"),
        ],
    }


# ---- service：创建 / 列表 / 详情 ----

def test_create_suite_from_golden_roundtrip():
    """未传 import_doc 时快照真实 golden（150 条、version 3.0.0、sha256）。"""
    s = SessionTest()
    item = ec.create_suite_from_golden(s, name=_rnd("golden"), changelog="M11")
    assert item["case_count"] == 150
    assert item["version"] == "3.0.0"
    assert len(item["checksum"]) == 64
    # G21（voice 预留标签）用例必须保留 skip_if_unsupported 且非终态预期
    cases = ec.get_suite_cases(s, item["id"])
    g21 = next(c for c in cases if c["case_id"] == "G21")
    assert g21["constraints"]["skip_if_unsupported"] == "voice"
    assert g21["expected"]["current_status"] != "COMPLETED"


def test_create_suite_import_doc_case_count():
    s = SessionTest()
    doc = _greedy_doc()
    doc["dataset_version"] = "2.0.0"
    item = ec.create_suite_from_golden(s, name=_rnd("g2"), changelog="imp",
                                       import_doc=doc)
    assert item["case_count"] == 2
    cases = ec.get_suite_cases(s, item["id"])
    assert [c["case_id"] for c in cases] == ["G01", "G02"]
    assert cases[0]["constraints"]["forbidden_nodes"] == []


def test_list_suites_and_get_suite():
    s = SessionTest()
    _seed_suite(s)
    _seed_suite(s)
    out = ec.list_suites(s, page=1, size=1)
    assert out["total"] >= 2
    assert len(out["items"]) == 1
    assert "case_count" in out["items"][0]

    item = ec.get_suite(s, out["items"][0]["id"])
    assert item is not None
    assert item["case_count"] >= 0


# ---- service：复制建新版（基线只读 + 红线校验）----

def test_copy_suite_preserves_baseline_immutable():
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    new = ec.copy_suite(s, src_item["id"], new_name=_rnd("copy"),
                        changelog="copy", owner_id="u-copy")
    assert new["id"] != src_item["id"]
    assert new["case_count"] == src_item["case_count"]
    # 基线 EvalCase 未被改动（完整性：case_ref_id 集合一致）
    src_cases = ec.get_suite_cases(s, src_item["id"])
    assert {c["case_id"] for c in src_cases} == {"G01", "G02"}


def test_copy_suite_applies_edits_and_idempotent_no_edit():
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    new = ec.copy_suite(s, src_item["id"], new_name=_rnd("copy-edited"),
                        changelog="edit G01 limit",
                        edits=[{"case_id": "G01", "fields": {"allow": False}}],
                        owner_id="u-copy")
    # 无编辑的纯复制到「同名校验和」已存在的地址（=源本身）-> 幂等拒绝
    with pytest.raises(ValueError, match="已存在"):
        ec.copy_suite(s, src_item["id"], new_name=src_item["name"], changelog="dup")
    new_cases = ec.get_suite_cases(s, new["id"])
    assert new_cases[0]["constraints"]["allow"] is False
    # 基线不变
    src_cases = ec.get_suite_cases(s, src_item["id"])
    assert src_cases[0]["constraints"].get("allow", True) is not False


def test_copy_suite_blocks_greedy_edit():
    """红线：改版复制时不允许把非 APPROVED 改成 SUCCESS 退款。"""
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    # G01 原 APPROVED/SUCCESS 是合法的；把 G01 改成 REJECTED/SUCCESS 红线拦截
    with pytest.raises(ValueError, match="仅 APPROVED 允许"):
        ec.copy_suite(s, src_item["id"], new_name=_rnd("bad"),
                      changelog="greedy",
                      edits=[{"case_id": "G01",
                              "fields": {"expected": {
                                  "current_status": "COMPLETED",
                                  "final_decision": "REJECTED",
                                  "refund_status": "SUCCESS"}}}],
                      owner_id="u-bad")


# ---- API：创建 / 列表 / 详情 / 用例 / 复制 / 删除 / RBAC ----

def api_login(client, username):
    return _auth_headers(login(client, username))


def test_api_leader_can_create_and_list(client):
    h = api_login(client, "leader1")
    r = client.post("/api/v1/evaluation-center/datasets",
                    json={"name": _rnd("suite"), "changelog": "M11 api"},
                    headers=h)
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["version"] == "3.0.0"
    assert item["case_count"] == 150

    out = client.get("/api/v1/evaluation-center/datasets", headers=h)
    assert out.status_code == 200
    assert out.json()["total"] >= 1


def test_api_copy_and_detail(client):
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    h = api_login(client, "leader1")
    r = client.post(f"/api/v1/evaluation-center/datasets/{src_item['id']}/copy",
                    json={"new_name": _rnd("copy"), "changelog": "copy",
                          "edits": [{"case_id": "G01",
                                     "fields": {"allow": False}}]},
                    headers=h)
    assert r.status_code == 201, r.text
    new = r.json()["item"]
    d = client.get(f"/api/v1/evaluation-center/datasets/{new['id']}", headers=h)
    assert d.status_code == 200
    assert d.json()["item"]["case_count"] == 2
    cases = client.get(f"/api/v1/evaluation-center/datasets/{new['id']}/cases", headers=h)
    assert cases.status_code == 200
    assert cases.json()["cases"][0]["constraints"]["allow"] is False


def test_api_delete_referenced_by_run_blocked(client):
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    run = models.EvalRun(
        run_id=f"run-{uuid.uuid4().hex[:10]}", task_type="refund_eval",
        schema_version="1.0", priority=0, status=models.EVAL_RUN_PENDING,
        suite_id=src_item["id"], suite_version=src_item["version"],
        dataset_checksum=src_item["checksum"],
    )
    s.add(run)
    s.commit()
    h = api_login(client, "admin1")
    r = client.delete(f"/api/v1/evaluation-center/datasets/{src_item['id']}",
                      headers=h)
    assert r.status_code == 409


def test_api_rbac_leader_write_customer_read_forbidden_delete(client):
    s = SessionTest()
    src_item = ec.create_suite_from_golden(s, name=_rnd("src"), changelog="base",
                                           import_doc=_greedy_doc())
    leader_h = api_login(client, "leader1")
    # leader 可创建/复制/读，但不能删除
    del_r = client.delete(f"/api/v1/evaluation-center/datasets/{src_item['id']}",
                          headers=leader_h)
    assert del_r.status_code == 403
    # customer_service（低权限）读列表被拒（require_min_role(supervisor)）
    cs_h = api_login(client, "customer_service1")
    r = client.get("/api/v1/evaluation-center/datasets", headers=cs_h)
    assert r.status_code == 403
    # admin 可删除未引用数据集
    adm_h = api_login(client, "admin1")
    r2 = client.delete(f"/api/v1/evaluation-center/datasets/{src_item['id']}",
                       headers=adm_h)
    assert r2.status_code == 204


def test_api_supervisor_can_read_and_start_one_click_but_cannot_govern(client, monkeypatch):
    """主管能执行只读全量评测；数据集治理仍须 leader，客服仍被拒绝。"""
    s = SessionTest()
    # 消除测试库上次中断遗留的一键任务，确保本次覆盖真正的创建与入队路径。
    for row in s.query(models.EvalRun).filter(
            models.EvalRun.status.in_([models.EVAL_RUN_PENDING, models.EVAL_RUN_RUNNING])).all():
        if bool((row.config or {}).get("one_click")):
            row.status = models.EVAL_RUN_CANCELLED
    s.commit()
    ec.create_suite_from_golden(s, name=_rnd("supervisor-suite"), changelog="RBAC",
                                import_doc=_greedy_doc())

    queued = []
    monkeypatch.setattr(eval_routes, "xadd", lambda stream, payload: queued.append((stream, payload)) or "1-0")

    supervisor_h = api_login(client, "supervisor1")
    assert client.get("/api/v1/evaluation-center/datasets", headers=supervisor_h).status_code == 200

    started = client.post("/api/v1/evaluation-center/runs/one-click", headers=supervisor_h)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["deduplicated"] is False
    assert body["status"] == models.EVAL_RUN_PENDING
    assert len(queued) == 1

    created = s.query(models.EvalRun).filter_by(run_id=body["run_id"]).first()
    s.refresh(created)
    assert created.config["one_click"] is True
    assert created.config["limit"] == 0

    denied_write = client.post(
        "/api/v1/evaluation-center/datasets",
        json={"name": _rnd("forbidden"), "changelog": "must remain leader-only"},
        headers=supervisor_h,
    )
    assert denied_write.status_code == 403

    customer_h = api_login(client, "customer_service1")
    denied_run = client.post("/api/v1/evaluation-center/runs/one-click", headers=customer_h)
    assert denied_run.status_code == 403

    created.status = models.EVAL_RUN_CANCELLED
    s.commit()
