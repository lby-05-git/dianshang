# -*- coding: utf-8 -*-
"""V2.0 数据集类别重分类脚本（实施计划 ）。

目标：把冻结 golden 150 条（G01–G150，DATASET_VERSION "3.0.0"）按类别
（core / boundary / anomaly / concurrency / security / voice）重分类，并把分类
写入独立映射表 `dataset_category_map`——**不修改 golden 冻结内容/校验和**。

设计要点
--------
- add-only 红线：本脚本只写 `dataset_category_map` 新表，不改 G01-G150 任何字段。
- 类别判定以 `case_type` 为主键，`tags` 为辅（reserved_* → security/intent/voice/coaching）。
- 类别 >= 30 条验收：核心类核心安全 100% 待 Phase 2 评测中心报告核对，
  此处只保证分类落库与回读。
- 幂等：按 (suite_id, case_ref_id) upsert；重复运行不产生重复行。

用法
----
    python scripts/refresh_dataset.py            # 写 dev/当前 DB
    python scripts/refresh_dataset.py --check    # 只读校验，不落库
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

# ---- 允许从项目根或 backend/ 两个目录运行 ----
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isdir(os.path.join(_ROOT, "eval_refund")):
    sys.path.insert(0, _ROOT)          # 项目根：……/多Agent 协同项目 -1-
else:
    sys.path.insert(0, os.path.dirname(_ROOT))   # backend/ 上一级
# backend/ 自身也加入 path，便于从 scripts/ 运行导入 app.*
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(_BACKEND) and _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from eval_refund import golden  # noqa: E402

from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

V2_DATASET_CATEGORIES = (
    "core", "boundary", "anomaly", "concurrency", "security", "voice",
)


# ---- 类别字典：case_type -> (category, 说明) ----
CASE_TYPE_CATEGORY = {
    # 核心业务主链路（审批/拒绝/幂等/核验一致）
    "auto_approve": ("core", "主链路自动审批"),
    "high_value_approve": ("core", "高金额审批"),
    "high_value_reject": ("core", "高金额拒绝"),
    "verification_all_match": ("core", "核验全匹配"),
    "refund_status_success": ("core", "退款成功回执"),
    "channel_cs_agent": ("core", "客服代提"),
    "channel_mini_program": ("boundary", "小程序渠道"),
    "refund_status": ("core", "三态一致"),
    "refund_success": ("core", "退款成功"),
    "human_approve_release": ("core", "人工审批放行"),
    "high_value_approve_leader": ("core", "leader 级审批"),
    "high_value_reject_leader": ("core", "leader 级拒绝"),
    # 边界
    "threshold_carding": ("boundary", "阈值卡控"),
    "threshold_carding_boundary": ("boundary", "阈值临界"),
    "partial_refund": ("boundary", "部分退款"),
    "idempotency": ("concurrency", "幂等重放"),
    "idempotent_recreate": ("concurrency", "幂等重建"),
    "refund_retry": ("anomaly", "退款重试"),
    # 异常
    "product_mismatch": ("anomaly", "商品不一致"),
    "product_mismatch_sku": ("anomaly", "SKU 不一致"),
    "price_mismatch": ("anomaly", "价格不一致"),
    "price_mismatch_partial_refunded": ("anomaly", "价格不一致-部分退款"),
    "order_not_found": ("anomaly", "订单不存在"),
    "owner_mismatch": ("anomaly", "归属不符"),
    "ocr_low_confidence": ("anomaly", "OCR 低置信"),
    "ocr_failure": ("anomaly", "OCR 失败"),
    "fraud_evidence_reuse": ("anomaly", "凭证复用欺诈"),
    "provider_timeout": ("anomaly", "Provider 超时"),
    "provider_unavailable": ("anomaly", "Provider 不可用"),
    "llm_unavailable": ("anomaly", "LLM 不可用"),
    "system_failure_no_refund": ("anomaly", "系统故障不退款"),
    # 保留：安全/意图/语音/陪练（tags 驱动）
    "reserved_security": ("security", "安全能力预留"),
    "reserved_intent": ("security", "意图预留（安全回落）"),
    "reserved_voice": ("voice", "语音预留"),
    "reserved_coaching": ("voice", "陪练预留"),
    "skipped_unsupported": ("security", "不支持回落安全"),
}

# 走 ensure-case_type 的额外映射（部分 case_type 未列，回退按 case_type 前缀）
_TAG_TO_CATEGORY = {
    "security": "security",
    "intent": "security",
    "voice": "voice",
    "coaching": "voice",
}


def classify_case(case: dict) -> tuple[str, str]:
    """返回 (category, reason)。优先 case_type，次优 tags。"""
    ct = case.get("case_type") or ""
    if ct in CASE_TYPE_CATEGORY:
        return CASE_TYPE_CATEGORY[ct]
    tags = case.get("tags") or []
    for t in tags:
        if t in _TAG_TO_CATEGORY:
            return _TAG_TO_CATEGORY[t], f"tag:{t}"
    # 保底：按 case_type 关键词
    for kw, cat in (
        ("idempot", "concurrency"),
        ("retry", "anomaly"),
        ("mismatch", "anomaly"),
        ("not_found", "anomaly"),
        ("timeout", "anomaly"),
        ("unavailable", "anomaly"),
        ("failure", "anomaly"),
    ):
        if kw in ct:
            return cat, f"case_type:{ct}"
    return "core", "default"


V2_SUITE_NAME = "golden-v2-baseline"  # 若需区分 V2.0 重新分类套件则改名


def _case_ref_id(case: dict) -> str:
    return case.get("id") or case.get("case_id") or ""


def _canonical_db() -> object:
    """从 backend/ 建引擎（与 app.infra 同数据库）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(_ROOT), "backend"))
    from app.infrastructure.db import DB_URL  # noqa: E402

    return create_engine(DB_URL, pool_pre_ping=True)


def collect(cases) -> list[tuple[str, str, str]]:
    """返回 [(case_ref_id, category, reason)]。"""
    out: list[tuple[str, str, str]] = []
    for c in cases:
        cat, reason = classify_case(c)
        out.append((_case_ref_id(c), cat, reason))
    return out


def build_dataset(cases) -> dict:
    """构造重分类数据集（不改 golden 冻结内容，只加 category）。"""
    doc = dict(golden.GOLDEN_META.copy() if hasattr(golden, "GOLDEN_META") else {})
    doc["version"] = "3.0.0"
    doc.setdefault("reserved_tags", list(golden.reserved_tags if hasattr(golden, "reserved_tags") else []))
    doc["cases"] = []
    for c in cases:
        cat, _ = classify_case(c)
        item = dict(c)
        item["category"] = cat
        doc["cases"].append(item)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="V2.0 数据集重分类")
    ap.add_argument("--check", action="store_true", help="只读校验，不落库")
    ap.add_argument("--suite-id", default=None, help="指定 suite_id；默认复用 golden 基线套件")
    args = ap.parse_args()

    cases = golden.GOLDEN_CASES if hasattr(golden, "GOLDEN_CASES") else golden.CASES
    rows = collect(cases)

    if args.check:
        cnt = Counter(c for _, c, _ in rows)
        print("== 类别统计（只读）==")
        for k in V2_DATASET_CATEGORIES:
            print(f"  {k:12s}: {cnt.get(k, 0)}")
        print("== 未分类（应能全部归类）==")
        un = [r for r in rows if r[1] == "core" and r[2] == "default"]
        print(f"  默认 core（case_type 未收录）: {len(un)}")
        for r in un[:20]:
            print("   ", r[0], r[2])
        # 验收：分类可区分 + 每类非零
        categorized = {k: cnt.get(k, 0) for k in V2_DATASET_CATEGORIES}
        ncat = sum(1 for v in categorized.values() if v)
        print(f"== {ncat}/6 类有归类 ==")
        return 0

    # ---- 落库（幂等，add-only）----
    from sqlalchemy.orm import Session  # noqa: F811
    from app.domain.models import DatasetCategoryMap  # noqa: F811
    from app.services import eval_center  # noqa: E402

    eng = _canonical_db()
    with Session(eng) as db:
        if args.suite_id:
            suite = db.get(eval_center.EvalSuite, args.suite_id)
            if suite is None:
                print(f"ERROR: suite_id {args.suite_id} 不存在")
                return 2
            suite_id = args.suite_id
        else:
            doc = build_dataset(cases)
            item = eval_center.create_suite_from_golden(
                db, name=V2_SUITE_NAME, changelog="V2.0 数据集重分类",
                import_doc=doc)
            suite_id = item["id"]

        # 写 dataset_category_map（按 (suite_id, case_ref_id) 幂等）
        inserted = skipped = 0
        for case_ref_id, cat, _reason in rows:
            exists = (db.query(DatasetCategoryMap)
                       .filter_by(suite_id=suite_id, case_ref_id=case_ref_id)
                       .first())
            if exists:
                skipped += 1
                continue
            db.add(DatasetCategoryMap(
                suite_id=suite_id, case_ref_id=case_ref_id, category=cat))
            inserted += 1
        db.commit()

    # ---- 汇报 ----
    with Session(eng) as db:
        seen = (db.query(DatasetCategoryMap.suite_id, DatasetCategoryMap.category)
                 .filter_by(suite_id=suite_id).all())
    cnt = Counter(c for _s, c in seen)
    print(f"suite_id={suite_id} inserted={inserted} skipped={skipped}")
    print("== 落库类别统计 ==")
    for k in V2_DATASET_CATEGORIES:
        print(f"  {k:12s}: {cnt.get(k, 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())