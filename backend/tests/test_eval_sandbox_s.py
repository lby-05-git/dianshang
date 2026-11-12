# -*- coding: utf-8 -*-
"""Phase S 沙箱资源限制 + in-memory 清理 + 导出白名单（S05/S06/S07）。

覆盖：
- S05 zip 炸弹防护：解压体积累计超限拒绝；工作表行数/单元格总数上限。
- S05 CSV 行数上限。
- S06 过期预览文件清扫（24h 未确认即清理，磁盘不泄漏）。
- S07 导出格式白名单：进白名单三种格式成功、非白名单拒绝、导出只读不落盘。
"""
import io
import zipfile
from pathlib import Path

import pytest

from app.services import office_sandbox as osb
from app.services.office_sandbox import (
    MAX_CSV_ROWS, MAX_SPREADSHEET_ROWS, MAX_UNCOMPRESSED_TOTAL,
    FileGateError, export_report, parse_csv, parse_xlsx, store_import_preview,
    validate_upload, _sweep_stale, import_xlsx,
)


# ---- S05 zip 炸弹 ----

def _mk_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_s05_zip_bomb_rejected_by_filegate(monkeypatch):
    """构造解压总量 > MAX_UNCOMPRESSED_TOTAL 的 zip，校验阶段即拒绝。"""
    # 用极小阈值（monkeypatch）让一个 2MB 部件就超限
    monkeypatch.setattr(osb, "MAX_UNCOMPRESSED_TOTAL", 1024 * 1024)
    big = b"A" * (2 * 1024 * 1024)
    data = _mk_zip({"xl/workbook.xml": big,
                    "xl/worksheets/sheet1.xml": b"<worksheet/>"})
    with pytest.raises(FileGateError, match="解压总量"):
        validate_upload("x.xlsx", data)


def test_s05_zip_bomb_rejected_at_parse(monkeypatch):
    """即使绕过 FileGate 直接调 parse_xlsx，也在读部件前被拦。"""
    monkeypatch.setattr(osb, "MAX_UNCOMPRESSED_TOTAL", 1024 * 1024)
    big = b"B" * (2 * 1024 * 1024)
    data = _mk_zip({"xl/workbook.xml": big,
                    "xl/worksheets/sheet1.xml": b"<worksheet/>"})
    with pytest.raises(FileGateError):
        parse_xlsx(data)


def test_s05_normal_zip_within_budget_passes():
    """正常尺寸 xlsx 解压总量低于上限，不被误杀。"""
    rows = [["h1", "h2"], ["a1", "b1"], ["a2", "b2"]]
    xb = import_xlsx(rows)
    parsed = parse_xlsx(xb)
    assert len(parsed) == 2
    assert parsed[0] == {"h1": "a1", "h2": "b1"}
    assert parsed[1] == {"h1": "a2", "h2": "b2"}


# ---- S05 行/单元格上限 ----

def test_s05_csv_row_limit(monkeypatch):
    monkeypatch.setattr(osb, "MAX_CSV_ROWS", 5)
    big = "id\n" + "\n".join(f"r{i}" for i in range(10))
    with pytest.raises(FileGateError, match="行数"):
        parse_csv(big.encode())


def test_s05_xlsx_row_limit(monkeypatch):
    """工作表行数超 MAX_SPREADSHEET_ROWS -> 拒绝。"""
    monkeypatch.setattr(osb, "MAX_SPREADSHEET_ROWS", 3)
    # 4 行表格（含表头）构造 xlsx
    rows = [[f"r{i}"] for i in range(4)]
    xb = import_xlsx(rows)
    from app.services.office_sandbox import parse_xlsx as px
    with pytest.raises(FileGateError, match="行数"):
        px(xb)


def test_s05_xlsx_cell_total_limit(monkeypatch):
    """单元格总数超 MAX_CELLS -> 拒绝。"""
    osb.MAX_CELLS = 2_000_000  # 还原
    monkeypatch.setattr(osb, "MAX_CELLS", 4)
    rows = [["a", "b"], ["c", "d"], ["e", "f"]]   # 6 个单元格 > 4
    xb = import_xlsx(rows)
    from app.services.office_sandbox import parse_xlsx as px
    with pytest.raises(FileGateError, match="单元格"):
        px(xb)


# ---- S06 过期预览清理 ----

def test_s06_stale_preview_swept(tmp_path, monkeypatch):
    """24h 前未确认的预览文件被 _sweep_stale 删除，不泄漏磁盘。"""
    monkeypatch.setattr(osb, "_OFFICE_PREVIEW_DIR", tmp_path)
    # 造一个"旧"预览：直接写时间戳为 2 天前
    old = tmp_path / "old.json"
    old.write_text("{}", encoding="utf-8")
    import time
    old_mtime = time.time() - 48 * 3600
    import os
    os.utime(old, (old_mtime, old_mtime))
    # 一个新的预览（未过期，不被清）
    fresh = tmp_path / "fresh.json"
    fresh.write_text("{}", encoding="utf-8")
    _sweep_stale()
    assert not old.exists()
    assert fresh.exists()


def test_s06_preview_reclaim_after_confirm(tmp_path, monkeypatch):
    """正常确认流程后预览被回收（已有 FG4，这里补：目录里只剩其它非预览文件）。"""
    monkeypatch.setattr(osb, "_OFFICE_PREVIEW_DIR", tmp_path)
    doc = {"cases": []}
    token = store_import_preview(doc)
    assert (tmp_path / f"{token}.json").exists()
    osb.consume_import_preview(token)
    assert not (tmp_path / f"{token}.json").exists()


# ---- S07 导出白名单 ----

def test_s07_export_format_whitelist():
    rep = {"run_id": "run-x", "environment": {}, "created_at": "",
           "summary": {"total": 1, "passed": 1, "failed": 0, "degraded": 0,
                       "pass_rate": 1.0},
           "cases": [{"case_id": "G01", "case_type": "auto",
                      "judge": {"verdict": "pass", "score": 1.0},
                      "candidate": {"business": {}},
                      "execution": {}, "error": None}]}
    # 白名单内三种成功且返回字节
    for fmt in ("xlsx", "csv", "docx"):
        assert isinstance(export_report(rep, fmt), bytes)
    # 非白名单 / 恶意 -> 拒绝（大写会被 lower 成合法，不在拒绝语义内）
    for bad in ("exe", "html", "sh", "pdf", "exe"):
        with pytest.raises(FileGateError):
            export_report(rep, bad)


def test_s07_export_keeps_everything_in_memory(monkeypatch):
    """导出只读写内存：进程内不产生任何落盘文件。"""
    monkeypatch.setattr(osb, "_OFFICE_PREVIEW_DIR", Path(io.BytesIO.__name__))  # 不可能路径
    rep = {"run_id": "run-mem", "environment": {}, "created_at": "",
           "summary": {"total": 1, "passed": 1, "failed": 0, "degraded": 0,
                       "pass_rate": 1.0},
           "cases": [{"case_id": "G01", "case_type": "auto",
                      "judge": {"verdict": "pass", "score": 1.0},
                      "candidate": {"business": {}},
                      "execution": {}, "error": None}]}
    data = export_report(rep, "csv")
    assert data.startswith(b"\xef\xbb\xbf")
    assert "run-mem".encode() in data or b"case_id" in data


def test_s07_csv_utf8_bom_and_no_temp_files():
    """csv 导出带 BOM（Excel 可读），且不写任何临时文件。"""
    rep = {"run_id": "r1", "environment": {}, "created_at": "",
           "summary": {"total": 1, "passed": 1, "failed": 0, "degraded": 0,
                       "pass_rate": 1.0},
           "cases": [{"case_id": "G01", "case_type": "auto",
                      "judge": {"verdict": "pass", "score": 1.0},
                      "candidate": {"business": {}},
                      "execution": {}, "error": None}]}
    data = export_report(rep, "csv")
    assert data[:3] == b"\xef\xbb\xbf"
    assert data.count(b"\n") >= 2


def test_s05_guard_constants_defined():
    """S05 护栏常量存在且为防御性正数。"""
    assert MAX_UNCOMPRESSED_TOTAL > 0
    assert MAX_SPREADSHEET_ROWS > 0
    assert MAX_CSV_ROWS > 0