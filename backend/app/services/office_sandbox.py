# -*- coding: utf-8 -*-
"""Office 安全沙箱（M15）：CSV/XLSX 导入解析 + XLSX/DOCX 报告导出 + FileGate。

红线：
- 所有文件处理都在内存 + 进程内完成，绝不执行外部命令、绝不开 shell；
- FileGate 前置校验：扩展名白名单、大小上限、路径穿越拒绝、** 伪造扩展名 **
  （zip 魔数 / CSV 文本嗅探）→ 未过闸一律拒绝，不进入解析；
- 导入在「预览后人工确认」才落库成数据集版本；预览/确认文件确认后即删除（100% 回收）；
- 文件内容只按结构解析（行列/单元格），内容里任何指令性文本都是数据，绝不执行。
- office_sandbox 只是解析器，不产生判定，不触碰 RefundCase。

不依赖第三方库（openpyxl/python-docx 均未安装）：XLSX/DOCX 用 zipfile + XML 最小读写。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# ---- FileGate 常量 ----
ALLOWED_EXT = {".csv", ".xlsx", ".xls", ".docx"}
MAX_FILE_BYTES = 10 * 1024 * 1024
# S05 资源护栏：zip 炸弹防护（解压总量/部件体积）+ 行/单元格上限。
# 进口压缩比可让 1MB zip 膨胀数百 MB；解压前做未压缩体积累计检查拦截。
MAX_UNCOMPRESSED_TOTAL = 64 * 1024 * 1024   # zip 内所有部件解压总量上限（64MB）
MAX_SPREADSHEET_ROWS = 200_000              # 单工作表行数上限
MAX_CELLS = 2_000_000                       # 单工作表单元格上限
MAX_CSV_ROWS = 200_000                      # CSV 行数上限
_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS = {"m": _NAMESPACE}
_PK_HEAD = b"PK\x03\x04"
_OFFICE_PREVIEW_DIR = Path(__file__).resolve().parents[3] / "uploads" / "office_previews"


class FileGateError(ValueError):
    """FileGate 拒绝原因（映射到客户端 400）。"""


# ================= FileGate =================

def validate_upload(filename: str | None, data: bytes) -> str:
    """FileGate 校验，返回规范化后的安全文件名（不含路径）。

    校验：扩展名白名单 → basename（去路径）→ 大小上限 → 伪造扩展名（zip 魔数/文本）。
    """
    name = Path(filename or "").name
    if name != (filename or ""):
        raise FileGateError("文件名不得包含路径分隔符")
    if not name or name in (".", ".."):
        raise FileGateError("非法的空文件名")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise FileGateError(f"扩展名不支持: {ext or '(无)'}，仅允许 {'/'.join(sorted(ALLOWED_EXT))}")
    if len(data) > MAX_FILE_BYTES:
        raise FileGateError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 限制")
    _check_magic(name, data, ext)
    return name


def _check_magic(filename: str, data: bytes, ext: str) -> None:
    """伪造扩展名嗅探：zip 族（xlsx/xls/docx）必须以 PK 头开始；csv 必须为文本。"""
    looks_zip = data[:4] == _PK_HEAD
    if ext == ".csv":
        if looks_zip:
            raise FileGateError("扩展名为 csv，但文件是 zip（疑似伪造扩展名）")
        _assert_text(data)
    elif ext in (".xlsx", ".xls", ".docx"):
        if not looks_zip:
            raise FileGateError(f"扩展名为 {ext}，但文件不是 zip 容器（疑似伪造扩展名）")
        if not _zip_contains(data):
            raise FileGateError("zip 容器内缺少 Office 部件（疑似伪造扩展名）")
    # 其余 ext 在上层已拒绝


def _assert_text(data: bytes) -> None:
    """csv 必须是可打印文本（utf-8-sig 尝试解码）。"""
    try:
        head = data[:4000].decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileGateError("csv 不是有效 UTF-8 文本")
    if "\x00" in head:
        raise FileGateError("csv 含空字节，疑似二进制文件")


def _zip_contains(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            _check_zip_bomb(z)
            names = z.namelist()
            return bool(names) and any(
                n.startswith(("xl/", "word/")) for n in names
            )
    except FileGateError:
        raise
    except (zipfile.BadZipFile, ValueError):
        return False


# ================= S05 zip 炸弹/资源护栏 =================

def _check_zip_bomb(z: zipfile.ZipFile) -> None:
    """解压前检查 zip 内所有部件的未压缩体积累计，超过则拒绝（zip 炸弹防护）。

    只看 central directory 的 file_size，不做任何 inflate，天然防解压放大。
    注意用 getinfo（名为 CRC/大小校验），避免 reading 才触发解压。
    """
    total = 0
    for info in z.infolist():
        total += info.file_size
        if total > MAX_UNCOMPRESSED_TOTAL:
            raise FileGateError(
                f"zip 解压总量将超过 {MAX_UNCOMPRESSED_TOTAL // (1024 * 1024)}MB 上限")
    return None


# ================= CSV 导入 =================

def parse_csv(data: bytes) -> list[dict]:
    """解析 CSV 为行字典（首行为表头）。UTF-8-sig 兼容 BOM。"""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for raw in reader:
        row = {str(k).strip(): ("" if v is None else str(v).strip())
               for k, v in raw.items() if k is not None}
        if any(v for v in row.values()):
            rows.append(row)
            if len(rows) > MAX_CSV_ROWS:
                raise FileGateError(f"CSV 行数超过 {MAX_CSV_ROWS} 上限")
    if not rows:
        raise FileGateError("CSV 无数据行")
    return rows


# ================= XLSX 导入（stdlib zip+XML 最小读）=================

_COMMENT_RE = re.compile(r"<[^>]+>")
_SHARED_OVER_DONE: bool = False


def _sst(z: zipfile.ZipFile) -> list[str]:
    """读取 xl/sharedStrings.xml -> 字符串列表。"""
    out: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.iter("{%s}si" % _NAMESPACE):
            out.append("".join(t.text or "" for t in si.iter("{%s}t" % _NAMESPACE)))
    return out


def parse_xlsx(data: bytes) -> list[dict]:
    """最小 XLSX 读：第一个 worksheet，首行为表头 -> 行字典。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            _check_zip_bomb(z)
            sheets = sorted(n for n in z.namelist()
                            if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            if not sheets:
                raise FileGateError("XLSX 缺少工作表")
            sst = _sst(z)
            root = ET.fromstring(z.read(sheets[0]))
            rows: list[list[tuple[str, str]]] = []  # (col_letter, value)
            total_cells = 0
            for row in root.iter("{%s}row" % _NAMESPACE):
                if len(rows) >= MAX_SPREADSHEET_ROWS:
                    raise FileGateError(f"XLSX 行数超过 {MAX_SPREADSHEET_ROWS} 上限")
                cells: list[tuple[str, str]] = []
                for c in row.iter("{%s}c" % _NAMESPACE):
                    total_cells += 1
                    if total_cells > MAX_CELLS:
                        raise FileGateError(f"XLSX 单元格总数超过 {MAX_CELLS} 上限")
                    ref = c.get("r") or ""
                    letter = _col_letter(ref)
                    v = c.find("{%s}v" % _NAMESPACE)
                    val = ""
                    if c.get("t") == "s" and v is not None:
                        idx = _to_int(v.text)
                        val = sst[idx] if idx is not None and idx < len(sst) else ""
                    elif c.get("t") == "inlineStr":
                        is_el = c.find("{%s}is" % _NAMESPACE)
                        if is_el is not None:
                            val = "".join(t.text or "" for t in is_el.iter("{%s}t" % _NAMESPACE))
                    elif v is not None:
                        val = v.text or ""
                    cells.append((letter, val))
                if cells:
                    rows.append(cells)
    except FileGateError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 结构损坏/缺部件 → 拒绝
        raise FileGateError(f"XLSX 解析失败: {exc.__class__.__name__}: {exc}")

    if not rows:
        raise FileGateError("XLSX 无数据行列")
    # 找表头（第一行非空单元格），列对齐到最大列号
    header_row = _row_to_vec(rows[0])
    cols = [h for h in header_row if h]
    if not cols:
        raise FileGateError("XLSX 首行为空，无表头")
    out: list[dict] = []
    for r in rows[1:]:
        vec = _row_to_vec(r)
        row = {}
        for i, h in enumerate(header_row):
            if not h:
                continue
            row[h] = vec[i] if i < len(vec) and vec[i] is not None else ""
        if any(v for v in row.values()):
            out.append(row)
    if not out:
        raise FileGateError("XLSX 无数据行")
    return out


def _col_letter(ref: str) -> str:
    return re.sub(r"\d+$", "", ref)


def _row_to_vec(cells: list[tuple[str, str]]) -> list[str | None]:
    """按列号对齐：A、B、...、Z、AA... 到最大列。"""
    def col_idx(letter: str) -> int:
        n = 0
        for ch in letter.upper():
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    max_i = max((col_idx(l) for l, _ in cells), default=-1)
    vec: list[str | None] = [None] * (max_i + 1)
    for letter, val in cells:
        vec[col_idx(letter)] = val
    return vec


def _to_int(s: str | None) -> int | None:
    try:
        return int(s or "")
    except (ValueError, TypeError):
        return None


# ================= 导入文档定形（列名 -> golden doc）=================

_COLUMN_MAP = {
    # golden 字段 <- 支持的列名（中英都要，见 _normalize_col）
    "id": ("id", "case_id", "用例ID", "ID", "case编号"),
    "case_type": ("case_type", "用例类型"),
    "order_id": ("order_id", "订单号"),
    "requested_amount": ("requested_amount", "金额"),
    "reason": ("reason", "原因", "reason_codes"),
    "channel": ("channel", "渠道"),
    "expected_status": ("expected_status", "期望状态"),
    "final_decision": ("final_decision", "期望决策"),
    "refund_status": ("refund_status", "期望退款状态"),
    "order_verification": ("order_verification", "期望订单核验"),
    "product_consistency": ("product_consistency", "期望商品核验"),
    "price_consistency": ("price_consistency", "期望价格核验"),
    "required_nodes": ("required_nodes", "必经节点", "required_nodes"),
    "forbidden_nodes": ("forbidden_nodes", "禁止节点"),
    "side_effect": ("side_effect", "副作用"),
    "allow_final_decisions": ("allow_final_decisions", "允许决策"),
    "tags": ("tags", "标签"),
    "skip_if_unsupported": ("skip_if_unsupported", "未实现跳过"),
}


def _norm_col(h: str) -> str:
    h = h.strip().lower().replace("_", "")
    for key, alts in _COLUMN_MAP.items():
        if h in (a.lower().replace("_", "") for a in alts):
            return key
    return h


def imported_rows_to_doc(rows: list[dict]) -> dict:
    """把导入行（CSV/XLSX 统一行字典）定形为 golden 文档结构。

    枚举错误全部抛 ValueError（转 400）；资金副作用红线由 _validate_doc 兜底，
    导入不做任何放宽。
    """
    cases: list[dict] = []
    for i, raw in enumerate(rows, start=1):
        norm = {_norm_col(k): v for k, v in raw.items()}
        expected = {
            "current_status": norm.get("expected_status") or "COMPLETED",
            "final_decision": norm.get("final_decision") or "PENDING",
            "refund_status": norm.get("refund_status") or "NONE",
        }
        if norm.get("order_verification"):
            expected["order_verification"] = norm["order_verification"]
        if norm.get("product_consistency"):
            expected["product_consistency"] = norm["product_consistency"]
        if norm.get("price_consistency"):
            expected["price_consistency"] = norm["price_consistency"]
        allow = {"final_decisions": [norm.get("final_decision") or "PENDING"],
                 "refund_status": [norm.get("refund_status") or "NONE"]}
        case = {
            "id": norm.get("id") or f"IMP{i:02d}",
            "case_type": norm.get("case_type") or "imported",
            "order_id": norm.get("order_id") or f"ORD-IMP-{i}",
            "requested_amount": norm.get("requested_amount") or "0.00",
            "reason": norm.get("reason") or "导入用例",
            "channel": norm.get("channel") or "",
            "expected": expected,
            "allow": allow,
            "tags": [t.strip() for t in (norm.get("tags") or "").split(",") if t.strip()],
            "required_nodes": _split_list(norm.get("required_nodes")),
            "forbidden_nodes": _split_list(norm.get("forbidden_nodes")),
            "side_effect": norm.get("side_effect") or "no_refund",
            "skip_if_unsupported": (norm.get("skip_if_unsupported").strip()
                                    if norm.get("skip_if_unsupported") else None),
        }
        cases.append(case)
    return {
        "schema_version": "1.0",
        "dataset_version": "2.0.0",
        "case_count": len(cases),
        "reserved_tags": [],
        "tags": {"source": "office_import", "owner": "eval_center"},
        "modalities": ["text"],
        "required_capabilities": {"target_type": "refund_workflow"},
        "cases": cases,
    }


def _split_list(v: str | None) -> list[str]:
    if not v:
        return []
    return [s.strip() for s in str(v).split(",") if s.strip()]


# ================= 预览暂存 / 100% 回收 =================

def store_import_preview(doc: dict) -> str:
    """把导入预览暂存为 uploads/office_previews/<token>.json，返回 token。"""
    p = _OFFICE_PREVIEW_DIR
    p.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:16]
    (p / f"{token}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return token


def load_import_preview(token: str) -> dict | None:
    fp = _OFFICE_PREVIEW_DIR / f"{token}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def consume_import_preview(token: str, doc: Any = None) -> None:
    """确认后删除预览文件（100% 回收）。同时清扫过期预览。"""
    fp = _OFFICE_PREVIEW_DIR / f"{token}.json"
    try:
        fp.unlink()
    except OSError:
        pass
    _sweep_stale()


def _sweep_stale() -> None:
    """清扫 24h 前未确认的预览文件，避免堆积。"""
    import time
    now = time.time()
    try:
        for f in _OFFICE_PREVIEW_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > 24 * 3600:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ================= XLSX 导出（stdlib zip+XML 最小写）=================

def rows_for_report(report: dict) -> list[list[str]]:
    """报告 -> 表格行（表头 + 每 case 一行）。"""
    s = report.get("summary") or {}
    head = ["case_id", "case_type", "verdict", "score", "key_status", "decision",
            "refund_status", "latency_ms", "error"]
    rows = [head]
    for c in report.get("cases") or []:
        judge = c.get("judge") or {}
        cand = c.get("candidate") or {}
        biz = cand.get("business") or {}
        rows.append([
            c.get("case_id", ""),
            c.get("case_type", ""),
            str(judge.get("verdict") or ""),
            str(judge.get("score") if judge.get("score") is not None else ""),
            str(biz.get("current_status") or ""),
            str(biz.get("final_decision") or ""),
            str(biz.get("refund_status") or ""),
            str((c.get("execution") or {}).get("latency_ms") or ""),
            str(c.get("error") or ""),
        ])
    return rows


def _build_xlsx_xml(rows: list[list[str]]) -> bytes:
    """生成 XLSX zip（不含共享字符串，直接内联字符串）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        col_letters = _col_letters(max(len(r) for r in rows) if rows else 0)
        sheet_rows = []
        for r_i, row in enumerate(rows, start=1):
            cells = []
            for c_i, val in enumerate(row):
                cells.append(f'<c r="{col_letters[c_i]}{r_i}" t="inlineStr">'
                             f'<is><t xml:space="preserve">{_xml_escape(str(val))}</t></is></c>')
            sheet_rows.append(f'<row r="{r_i}">{"".join(cells)}</row>')
        sheet1 = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  f'<worksheet xmlns="{_NAMESPACE}"><sheetData>'
                  f'{"".join(sheet_rows)}</sheetData></worksheet>')
        workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="Report" sheetId="1" '
                    'r:id="rId1"/></sheets></workbook>')
        content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                         'package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
                         'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
                         'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                   '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
    return buf.getvalue()


def _col_letters(n: int) -> list[str]:
    out = []
    for i in range(n):
        s = ""
        i2 = i + 1
        while i2:
            i2, r = divmod(i2 - 1, 26)
            s = chr(65 + r) + s
        out.append(s)
    return out


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def import_xlsx(rows: list[list[str]]) -> bytes:
    return _build_xlsx_xml(rows)


# ================= DOCX 导出（stdlib zip+XML 最小写）=================

def export_docx(report: dict) -> bytes:
    """把报告聚合导出为 .docx（段落式摘要 + case 明细）。"""
    s = report.get("summary") or {}
    lines = [
        f"评测报告 {report.get('run_id', '')}",
        f"数据集版本: {(report.get('environment') or {}).get('dataset_version', '')}",
        f"创建时间: {report.get('created_at', '')}",
        "",
        f"总计 {s.get('total', 0)}，通过 {s.get('passed', 0)}，失败 {s.get('failed', 0)}，"
        f"degraded {s.get('degraded', 0)}，pass_rate {s.get('pass_rate', 0)}",
        "",
    ]
    for c in report.get("cases") or []:
        judge = c.get("judge") or {}
        lines.append(f"- {c.get('case_id', '')} [{c.get('case_type', '')}] "
                     f"verdict={judge.get('verdict', '')}")
    body = "".join(f"<w:p><w:r><w:t>{_xml_escape(ln)}</w:t></w:r></w:p>" for ln in lines)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{body}</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                     'package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.'
                     'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def export_report(report: dict, fmt: str) -> bytes:
    """按格式导出报告（fmt: xlsx / docx / csv）。"""
    fmt = fmt.lower()
    if fmt == "csv":
        rows = rows_for_report(report)
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerows(rows)
        return sio.getvalue().encode("utf-8-sig")
    if fmt == "xlsx":
        return import_xlsx(rows_for_report(report))
    if fmt == "docx":
        return export_docx(report)
    raise FileGateError("不支持的导出格式")