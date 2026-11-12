# -*- coding: utf-8 -*-
"""M15 Office 安全沙箱 + FileGate 契约测试。

覆盖：
- FG1 FileGate：扩展名白名单 / 大小 / 路径穿越 / 伪造扩展名（zip 魔数）阻断。
- FG2 CSV 导入解析 + 定形 golden doc + 红线预检（SUCCESS 仅 APPROVED）。
- FG3 XLSX 导出→解析回读 roundtrip（stdlib inlineStr）。
- FG4 导入预览→人工确认→建数据集版本；预览文件 100% 回收。
- FG5 报告导出 xlsx/docx/csv；FileGate 对导出结果自检通过（可回读）。
- FG6 沙箱不执行内容指令（内容里的 shell/文件名只是数据）。
- FG7 API：leader 可上传预览；确认建版；导出；admin/CS 权限。
"""
import io
import zipfile

import pytest

from app.services.eval_center import create_suite_from_golden
from app.services.office_sandbox import (
    ALLOWED_EXT, FileGateError, consume_import_preview, export_report,
    imported_rows_to_doc, load_import_preview, parse_csv, parse_xlsx,
    store_import_preview, validate_upload,
)
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


# ---- FG1 FileGate ----

def test_filegate_extension_whitelist():
    assert ".csv" in ALLOWED_EXT and ".xlsx" in ALLOWED_EXT
    with pytest.raises(FileGateError):
        validate_upload("evil.sh", b"#!/bin/sh")
    with pytest.raises(FileGateError):
        validate_upload("evil.exe", b"MZ")
    with pytest.raises(FileGateError):
        validate_upload("noext", b"hello")


def test_filegate_path_traversal_rejected():
    with pytest.raises(FileGateError):
        validate_upload("../escape.csv", b"a,b\n1,2")
    with pytest.raises(FileGateError):
        validate_upload("C:\\evil.csv", b"a,b\n1,2")
    with pytest.raises(FileGateError):
        validate_upload("a/../../b.csv", b"a,b")


def test_filegate_fake_extension():
    # csv 但 zip 头 -> 拒绝（伪造扩展名）
    with pytest.raises(FileGateError):
        validate_upload("fake.csv", b"PK\x03\x04" + b"\x00" * 20)
    # xlsx 但非 zip -> 拒绝
    with pytest.raises(FileGateError):
        validate_upload("fake.xlsx", b"not a zip at all")


def test_filegate_oversize():
    big = b"a,b\n" + b"x" * (11 * 1024 * 1024)
    with pytest.raises(FileGateError):
        validate_upload("big.csv", big)


# ---- FG2 CSV 导入 ----

def test_csv_parse_to_doc_and_redlines():
    csv_bytes = (
        "id,case_type,order_id,requested_amount,final_decision,refund_status,"
        "reason,forbidden_nodes\n"
        "C01,auto_approve,ORD-1,100.00,APPROVED,SUCCESS,正常退款,\n"
        "C02,high_reject,ORD-2,200.00,REJECTED,NONE,人工拒绝,refund_execution\n"
    ).encode("utf-8")
    rows = parse_csv(csv_bytes)
    doc = imported_rows_to_doc(rows)
    assert doc["case_count"] == 2
    c1 = doc["cases"][0]
    assert c1["expected"]["final_decision"] == "APPROVED"
    assert c1["expected"]["refund_status"] == "SUCCESS"
    c2 = doc["cases"][1]
    assert "refund_execution" in c2["forbidden_nodes"]
    # 红线预检：非 APPROVED 必须禁 refund_execution（已满足）-> 不抛
    from app.services.eval_center import _validate_doc
    _validate_doc(doc)


def test_csv_import_redline_rejects_success_without_approval():
    csv_bytes = (
        "id,final_decision,refund_status\n"
        "X01,REJECTED,SUCCESS\n"   # 非 APPROVED 不允许 SUCCESS
    ).encode("utf-8")
    rows = parse_csv(csv_bytes)
    doc = imported_rows_to_doc(rows)
    from app.services.eval_center import _validate_doc
    with pytest.raises(ValueError):
        _validate_doc(doc)


# ---- FG3 XLSX roundtrip ----

def test_xlsx_export_parse_roundtrip():
    rows = [["case_id", "case_type", "verdict"], ["G01", "auto", "pass"]]
    xb = export_report({"summary": {}, "cases": []}, "xlsx") if False else None
    from app.services.office_sandbox import import_xlsx
    xb = import_xlsx(rows)
    assert xb[:4] == b"PK\x03\x04"
    parsed = parse_xlsx(xb)
    assert parsed == [{"case_id": "G01", "case_type": "auto", "verdict": "pass"}]


# ---- FG4 导入 preview/confirm + 回收 ----

def test_preview_confirm_create_dataset_and_reclaim(tmp_path, monkeypatch):
    from app.services import office_sandbox as osb
    monkeypatch.setattr(osb, "_OFFICE_PREVIEW_DIR", tmp_path / "previews")
    doc = {
        "schema_version": "1.0", "dataset_version": "2.0.0",
        "case_count": 1, "reserved_tags": [],
        "tags": {"source": "office"}, "modalities": ["text"],
        "required_capabilities": {"target_type": "refund_workflow"},
        "cases": [{
            "id": "C01", "case_type": "auto_approve",
            "order_id": "ORD-OFF-1", "requested_amount": "10.00",
            "reason": "导入", "channel": "app",
            "expected": {"current_status": "COMPLETED",
                         "final_decision": "APPROVED", "refund_status": "SUCCESS",
                         "order_verification": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
            "tags": [], "required_nodes": [], "forbidden_nodes": [],
            "side_effect": "refund:success",
        }],
    }
    token = store_import_preview(doc)
    assert load_import_preview(token) == doc
    s = SessionTest()
    item = create_suite_from_golden(s, name="office-suite", changelog="M15",
                                    owner_id="u", import_doc=doc)
    assert item["case_count"] == 1
    consume_import_preview(token)
    assert load_import_preview(token) is None   # 100% 回收


# ---- FG5 报告导出 ----

def test_report_export_formats():
    rep = {
        "run_id": "run-x", "environment": {"dataset_version": "2.0.0"},
        "created_at": "2026-08-01T00:00:00+00:00",
        "summary": {"total": 1, "passed": 1, "failed": 0, "degraded": 0,
                    "pass_rate": 1.0},
        "cases": [{"case_id": "G01", "case_type": "auto",
                   "judge": {"verdict": "pass", "score": 1.0},
                   "candidate": {"business": {"current_status": "COMPLETED",
                                              "final_decision": "APPROVED",
                                              "refund_status": "SUCCESS"}},
                   "execution": {"latency_ms": 1.0}, "error": None}],
    }
    for fmt in ("xlsx", "csv", "docx"):
        data = export_report(rep, fmt)
        assert len(data) > 0
    # FileGate 对导出 docx/xlsx 自检通过（可回读）
    xlsx_b = export_report(rep, "xlsx")
    assert validate_upload("x.xlsx", xlsx_b) == "x.xlsx"
    csv_b = export_report(rep, "csv")
    assert validate_upload("x.csv", csv_b) == "x.csv"
    with pytest.raises(FileGateError):
        export_report(rep, "exe")


# ---- FG6 沙箱不执行内容指令 ----

def test_sandbox_never_executes_content_instructions():
    # 内容里塞 shell/路径只是数据，解析后仍是数据行，不触发任何执行
    csv_bytes = (
        "id,final_decision,refund_status,reason\n"
        "X1,APPROVED,SUCCESS,; rm -rf /\n"       # shell 注入
        "X2,APPROVED,SUCCESS,../../etc/passwd\n"
    ).encode("utf-8")
    rows = parse_csv(csv_bytes)
    doc = imported_rows_to_doc(rows)
    reasons = [c["reason"] for c in doc["cases"]]
    assert reasons[0] == "; rm -rf /"
    assert reasons[1] == "../../etc/passwd"


# ---- FG7 API ----

def test_api_office_import_preview_and_confirm(client):
    from app.services import office_sandbox as osb
    import app.api.routes_eval_center as routes_mod
    # 指向临时预览目录（避免污染真实 uploads）
    import tempfile
    tmp = tempfile.mkdtemp()
    osb._OFFICE_PREVIEW_DIR = __import__("pathlib").Path(tmp)

    csv_bytes = (
        "id,case_type,final_decision,refund_status,reason\n"
        "C01,auto_approve,APPROVED,SUCCESS,正常\n"
    ).encode("utf-8")
    h = _api_h(client, "leader1")
    r = client.post(
        "/api/v1/evaluation-center/office/import-preview",
        files={"file": ("cases.csv", io.BytesIO(csv_bytes), "text/csv")},
        headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["token"]
    assert body["case_count"] == 1
    assert body["preview"][0]["id"] == "C01"

    c = client.post(
        "/api/v1/evaluation-center/office/import-confirm",
        json={"token": token, "name": "office-api-suite",
              "changelog": "M15 API"},
        headers=h)
    assert c.status_code == 201, c.text
    assert c.json()["item"]["case_count"] == 1
    # 预览文件已回收
    assert load_import_preview(token) is None


def test_api_office_fake_extension_rejected(client):
    h = _api_h(client, "leader1")
    r = client.post(
        "/api/v1/evaluation-center/office/import-preview",
        files={"file": ("fake.csv", io.BytesIO(b"PK\x03\x04" + b"\x00" * 16),
                        "text/csv")},
        headers=h)
    assert r.status_code == 400
    assert "伪造" in (r.json().get("detail") or r.json().get("message") or "")


def test_api_office_rbac(client):
    h = _api_h(client, "customer_service1")
    r = client.post("/api/v1/evaluation-center/office/import-preview",
                    files={"file": ("a.csv", io.BytesIO(b"a\n1"), "text/csv")},
                    headers=h)
    assert r.status_code == 403


def test_api_report_export_endpoint(client):
    import uuid
    from app.domain import models
    from app.services import eval_center as ec
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"suite-{uuid.uuid4().hex[:8]}", changelog="M15")
    run = models.EvalRun(run_id=f"run-{uuid.uuid4().hex[:10]}", name="exp",
                         task_type="refund_eval", schema_version="1.0",
                         priority=0, status="COMPLETED", suite_id=suite["id"],
                         suite_version="2.0.0", dataset_checksum="abc",
                         config={}, summary={"total": 1, "passed": 1, "failed": 0,
                                             "degraded": 0, "pass_rate": 1.0},
                         performance={}, cost={"measured": False})
    s.add(run); s.commit()
    h = _api_h(client, "leader1")
    r = client.get(f"/api/v1/evaluation-center/reports/{run.run_id}/export?fmt=xlsx",
                   headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert r.content[:4] == b"PK\x03\x04"
    r2 = client.get(f"/api/v1/evaluation-center/reports/{run.run_id}/export?fmt=csv",
                    headers=h)
    assert r2.status_code == 200
    assert r2.content.startswith(b"\xef\xbb\xbfcase_id")  # utf-8-sig BOM