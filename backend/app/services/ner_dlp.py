# -*- coding: utf-8 -*-
"""NER 识别 + 按角色/场景脱敏策略（V2.0 -3 DLP 增强；）。

从「4 类正则」升级为「NER 识别 + 策略脱敏」：
- 实体识别（确定性，不依赖 LLM，保证可复现）：
    基础 4 类（PHONE/ID_CARD/BANK_CARD/API_KEY）+ 扩展 4 类（EMAIL/ORDER_NO/NAME/ROLE_ID）；
    相比纯正则增加语境词约束（如 ORDER_NO 须伴随订单语境、NAME 须伴随姓名标记），降低误报。
- 脱敏策略（role × scenario 分级收紧）：
    role ∈ {customer, agent, officer}，scenario ∈ {refund, voice, paper, sandbox}；
    每类实体有基础动作（BASE_TYPE_ACTION），role/scenario 覆盖只允许「更严」，
    绝不允许放开（策略级别单调：FULL > HASH > PARTIAL > EDGE > NONE）。
- DLP 红线保持：非 NONE 实体掩码后明文绝不残留于结果；
    ORDER_NO 是唯一允许 NONE 的类型（业务标识，非人口属性），但 voice/sandbox/paper 仍强制 EDGE。
- 标注集评测 evaluate：给定带真值标签的语料，输出 准确率/召回率/误报率（验收）。

本模块只做识别 + 脱敏 + 报告；不落库、不改判定、不改任何业务状态。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from app.domain.models import (
    DLP_API_KEY,
    DLP_BANK_CARD,
    DLP_ID_CARD,
    DLP_PHONE,
)

# ---- NER 扩展实体类型（新类型常量仅本模块使用，不污染 domain 常量）----
DLP_EMAIL = "EMAIL"
DLP_ORDER_NO = "ORDER_NO"
DLP_NAME = "NAME"
DLP_ROLE_ID = "ROLE_ID"

ALL_ENTITY_TYPES = (
    DLP_PHONE, DLP_ID_CARD, DLP_BANK_CARD, DLP_API_KEY,
    DLP_EMAIL, DLP_ORDER_NO, DLP_NAME, DLP_ROLE_ID,
)

# ---- 脱敏动作等级（越高越严；策略覆盖只允许往高收紧）----
MASK_FULL = "FULL"        # 全掩：替换为【类型已掩码】
MASK_HASH = "HASH"        # 哈希：sha256 前 16 位（不可逆，可用于去重/审计）
MASK_PARTIAL = "PARTIAL"  # 部分掩：保留首尾（识别用）
MASK_EDGE = "EDGE"        # 仅保留末 4 位
MASK_NONE = "NONE"        # 不掩（仅 ORDER_NO 允许；业务标识必须保留才能处理）

_ACTION_RANK = {MASK_FULL: 4, MASK_HASH: 3, MASK_PARTIAL: 2, MASK_EDGE: 1, MASK_NONE: 0}


def _strictest(*actions: str) -> str:
    return max(actions, key=lambda a: _ACTION_RANK[a])


# 每类实体基础动作（全局最弱基准；role/scenario 只可加严）
BASE_TYPE_ACTION: dict[str, str] = {
    DLP_PHONE: MASK_PARTIAL,
    DLP_ID_CARD: MASK_PARTIAL,
    DLP_BANK_CARD: MASK_PARTIAL,
    DLP_API_KEY: MASK_PARTIAL,
    DLP_EMAIL: MASK_FULL,
    DLP_ORDER_NO: MASK_NONE,
    DLP_NAME: MASK_FULL,
    DLP_ROLE_ID: MASK_FULL,
}

# 场景收紧（加严覆盖；sandbox=陪练/沙箱环境绝不暴露真实数据）
SCENARIO_OVERRIDES: dict[str, dict[str, str]] = {
    "refund": {},   # 退款场景：基础策略即可
    "voice": {
        DLP_PHONE: MASK_FULL, DLP_EMAIL: MASK_FULL,
        DLP_ORDER_NO: MASK_EDGE, DLP_NAME: MASK_FULL,
    },
    "paper": {
        DLP_PHONE: MASK_FULL, DLP_ID_CARD: MASK_FULL, DLP_BANK_CARD: MASK_FULL,
        DLP_EMAIL: MASK_FULL, DLP_ORDER_NO: MASK_EDGE, DLP_NAME: MASK_FULL,
    },
    "sandbox": {
        DLP_PHONE: MASK_EDGE, DLP_ID_CARD: MASK_EDGE, DLP_BANK_CARD: MASK_EDGE,
        DLP_API_KEY: MASK_EDGE, DLP_EMAIL: MASK_FULL,
        DLP_ORDER_NO: MASK_EDGE, DLP_NAME: MASK_FULL, DLP_ROLE_ID: MASK_FULL,
    },
}

# 角色收紧（customer 视角对自己隐私更收紧）
ROLE_OVERRIDES: dict[str, dict[str, str]] = {
    "customer": {
        DLP_PHONE: MASK_FULL, DLP_ID_CARD: MASK_FULL, DLP_BANK_CARD: MASK_FULL,
        DLP_EMAIL: MASK_FULL, DLP_ORDER_NO: MASK_EDGE, DLP_NAME: MASK_FULL,
    },
    "agent": {},
    "officer": {},
}

VALID_ROLES = tuple(ROLE_OVERRIDES)
VALID_SCENARIOS = tuple(SCENARIO_OVERRIDES)


# ---- 实体识别正则（确定性 NER：正则可重现 + 语境约束降误报）----

# 手机：11 位大陆手机，词边界隔离
_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 身份证：18 位（17 数字 + X）
_RE_ID_CARD = re.compile(r"(?<!\d)(\d{17}[0-9Xx])(?!\d)")
# 银行卡：13-19 位数字
_RE_BANK_CARD = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
# API Key：sk- / AKIA / id:secret
# 注意：\b 在 Python unicode 模式下把汉字当单词字符，"密钥sk-…"（汉字紧贴）会
# 丢失边界导致漏检 —— 用 (?<![A-Za-z0-9_])/(?![A-Za-z0-9_]) 对称断言替代 \b。
_RE_API_KEY = re.compile(
    r"(?i)(?<![A-Za-z0-9_])((?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|"
    r"[A-Za-z0-9_]{8}:[A-Za-z0-9]{8,})(?![A-Za-z0-9_]))"
)
# 邮箱
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 订单号：须伴随订单语境（降低裸数字串误判）；中英双写法
_RE_ORDER_CN = re.compile(r"(?:订单号?|单号|序号)[:：\s]*([A-Za-z0-9][A-Za-z0-9_-]{5,23})")
_RE_ORDER_EN = re.compile(r"(?i)\b(ord(?:er)?[-_]?[0-9]{4,}|NO[-_.]?[0-9]{4,})\b")
# 姓名：须伴随姓名标记（交替项 longest-first，避免“客户名姓氏”吞入姓名）
_RE_NAME = re.compile(r"(?:客户姓名|客户名|姓名|我的姓名)[:：\s]*([一-龥]{2,4})")
_RE_NAME_I = re.compile(r"我叫([一-龥]{2,4})(?:[，。,;；\s]|$)")
# 伪身份（角色冒用）：Critic 语义层用 → 亦纳入 DLP 掩码（身份冒用字眼不入库）
_RE_ROLE_ID = re.compile(
    r"我是([一-龥]{2,8}(?:系统管理员|客户主管|老板|创始人|客服主管|管理员|经理|平台|客服))",
)


@dataclass
class NEREntity:
    """一条 NER 识别结果（含策略动作与掩码）。"""

    entity_type: str
    start: int
    end: int
    raw: str
    context: str
    action: str
    masked: str

    def as_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "position": self.start,
            "raw_len": len(self.raw),
            "context": self.context,
            "action": self.action,
            "masked": self.masked,
        }


# ---- 掩码构造（与基线格式一致：手机 138****0000 等）----

def _mask_phone(v: str) -> str:
    return f"{v[:3]}****{v[-4:]}"


def _mask_id_card(v: str) -> str:
    return f"{v[:6]}********{v[-4:]}"


def _mask_bank_card(v: str) -> str:
    return f"{v[:4]} ******** {v[-4:]}"


def _mask_api_key(v: str) -> str:
    if len(v) <= 8:
        return v[:2] + "****"
    return f"{v[:4]}****{v[-4:]}"


_FULL_MARKER: dict[str, str] = {
    DLP_PHONE: "【手机已掩码】",
    DLP_ID_CARD: "【身份证已掩码】",
    DLP_BANK_CARD: "【银行卡已掩码】",
    DLP_API_KEY: "【密钥已掩码】",
    DLP_EMAIL: "【邮箱已掩码】",
    DLP_NAME: "【姓名已掩码】",
    DLP_ROLE_ID: "【身份已掩码】",
    DLP_ORDER_NO: "【订单号已掩码】",
}

_PARTIAL_MASKER: dict[str, str] = {
    DLP_PHONE: _mask_phone,
    DLP_ID_CARD: _mask_id_card,
    DLP_BANK_CARD: _mask_bank_card,
    DLP_API_KEY: _mask_api_key,
}


def _apply_action(entity_type: str, raw: str, action: str) -> str:
    """对单个实体应用脱敏动作；返回掩码（掩码后明文消失）。"""
    if action == MASK_NONE:
        return raw
    if action == MASK_FULL:
        return _FULL_MARKER.get(entity_type, "【已掩码】")
    if action == MASK_HASH:
        return f"h${hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    if action == MASK_EDGE:
        if len(raw) <= 4:
            return _FULL_MARKER.get(entity_type, "【已掩码】")
        return "*" * (len(raw) - 4) + raw[-4:]
    # MASK_PARTIAL
    masker = _PARTIAL_MASKER.get(entity_type)
    if masker is not None:
        return masker(raw)
    return _FULL_MARKER.get(entity_type, "【已掩码】")


# ---- 识别 + 按策略脱敏 ----

def resolve_action(entity_type: str, role: str, scenario: str) -> str:
    """实体类型在该 role×scenario 下的生效动作（只可加严，不可放开）。"""
    base = BASE_TYPE_ACTION.get(entity_type, MASK_FULL)
    over = SCENARIO_OVERRIDES.get(scenario, {}).get(entity_type, MASK_NONE)
    r_over = ROLE_OVERRIDES.get(role, {}).get(entity_type, MASK_NONE)
    return _strictest(base, over, r_over)


def _context(text: str, start: int, end: int, pad: int = 4) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return f"...{text[lo:start]}[{text[start:end]}]{text[end:hi]}..."


def _extractors(text: str) -> list[NEREntity]:
    """确定性识别全部候选实体（尚未应用策略）。

    group>0 的规则取捕获组片段（截掉语境前缀如“订单号”以精确定位实体主体）；
    m.start(group)/m.end(group) 为绝对下标，直接使用、不再叠加 m.start()。
    """
    out: list[NEREntity] = []
    for pat, ftype, group in (
        (_RE_API_KEY, DLP_API_KEY, 1),
        (_RE_PHONE, DLP_PHONE, 1),
        (_RE_ID_CARD, DLP_ID_CARD, 1),
        (_RE_BANK_CARD, DLP_BANK_CARD, 1),
        (_RE_EMAIL, DLP_EMAIL, 0),
        (_RE_ORDER_CN, DLP_ORDER_NO, 1),
        (_RE_ORDER_EN, DLP_ORDER_NO, 0),
        (_RE_NAME, DLP_NAME, 1),
        (_RE_NAME_I, DLP_NAME, 1),
        (_RE_ROLE_ID, DLP_ROLE_ID, 0),
    ):
        for m in pat.finditer(text):
            if group:
                raw = m.group(group)
                s, e = m.start(group), m.end(group)
            else:
                raw = m.group(0)
                s, e = m.start(), m.end()
            out.append(NEREntity(
                entity_type=ftype,
                start=s,
                end=e,
                raw=raw,
                context=_context(text, m.start(), m.end()),
                action="", masked="",
            ))
    return out


def _disambiguate(ents: list[NEREntity], text: str) -> list[NEREntity]:
    """同 span 多类型消解（语境判别）：如 18 位身份证同时命中银行卡正则。

    身份证(18 位) 与 银行卡(13-19 位) 在裸数字串上重叠：
    - 语境含“身份证/证件” → 判 ID_CARD；
    - 语境含“银行卡/结算/到账” → 判 BANK_CARD；
    - 无语境则按位数：18 → ID_CARD，13-16 → BANK_CARD。
    其余同 span 冲突保留先进入的（PII 已在识别阶段优先）。
    """
    by_span: dict[tuple[int, int], list[NEREntity]] = {}
    for e in ents:
        by_span.setdefault((e.start, e.end), []).append(e)
    out: list[NEREntity] = []
    for (_s, _e), group in by_span.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        types = {x.entity_type for x in group}
        if DLP_ID_CARD in types and DLP_BANK_CARD in types:
            ctx = text[max(0, _s - 6): _e]
            mark_id = any(w in ctx for w in ("身份证", "证件", "身份", "登记"))
            mark_bank = any(w in ctx for w in ("银行卡", "卡号", "银行", "到账", "结算"))
            raw_len = len(group[0].raw)
            if mark_id:
                pick = DLP_ID_CARD
            elif mark_bank:
                pick = DLP_BANK_CARD
            elif raw_len == 18:
                pick = DLP_ID_CARD
            else:
                pick = DLP_BANK_CARD
            out.append(next(x for x in group if x.entity_type == pick))
            continue
        out.append(group[0])
    return out


def resolve_entities(
    text: str, *,
    role: str = "agent", scenario: str = "refund",
) -> list[NEREntity]:
    """识别 + 应用策略 + 去重叠。返回按 start 升序的实体。

    重叠消解：符号密集处的 PII 类型（PHONE/ID/BANK/API_KEY/EMAIL）优先，
    名称/号类让位；前一段内重叠的后段丢弃（干净、小步、防双重替换）。
    """
    cands = sorted(_extractors(text or ""), key=lambda e: (e.start, -len(e.raw)))
    PII_PRIORITY = {DLP_PHONE, DLP_ID_CARD, DLP_BANK_CARD, DLP_API_KEY, DLP_EMAIL}
    kept: list[NEREntity] = []
    for e in cands:
        e.action = resolve_action(e.entity_type, role=role, scenario=scenario)
        e.masked = _apply_action(e.entity_type, e.raw, e.action)
        new_kept: list[NEREntity] = []
        keep_me = True
        for k in kept:
            if e.start < k.end and e.end > k.start:
                # 重叠：普通 PII 优先于弱类型（只让 PII 顶掉弱类型，弱类型不胜出）
                if e.entity_type in PII_PRIORITY and k.entity_type not in PII_PRIORITY:
                    continue                    # 丢弃弱类型 k，保留 e
                keep_me = False                 # 丢弃 e，保留既存 k
                new_kept.append(k)
            else:
                new_kept.append(k)
        if keep_me:
            new_kept.append(e)
        kept = new_kept
    kept.sort(key=lambda x: x.start)
    return kept


def mask(text: str, *, role: str = "agent", scenario: str = "refund") -> tuple[str, list[NEREntity]]:
    """按 role×scenario 策略对文本脱敏。返回 (masked_text, entities)。

    DLP 红线：非 NONE 实体替换后明文绝不残留于 masked_text。
    """
    entities = resolve_entities(text, role=role, scenario=scenario)
    if not entities:
        return (text or "", [])
    masked = text or ""
    for e in reversed(entities):
        if e.action == MASK_NONE:
            continue
        masked = masked[:e.start] + e.masked + masked[e.end:]
    return masked, entities


# ---- 版本对比 / 一致性 ----

def policy_matrix() -> list[dict]:
    """工作台/文档展示：全 role×scenario 之下每类实体的生效动作。"""
    rows = []
    for role in VALID_ROLES:
        for scenario in VALID_SCENARIOS:
            rows.append({
                "role": role, "scenario": scenario,
                "actions": {t: resolve_action(t, role, scenario) for t in ALL_ENTITY_TYPES},
            })
    return rows


# ---- 标注集评测（准确率/召回率/误报率；验收）----

@dataclass
class LabeledDoc:
    text: str
    expected: set[str]          # 期望出现的实体类型（真值标签）

    # 逐类型出现次数（精准召回/误报用；可空 = 按集合判定）
    counts: dict[str, int] = field(default_factory=dict)


def evaluate(
    docs: Iterable[LabeledDoc], *,
    role: str = "agent", scenario: str = "refund",
) -> dict:
    """对标注集输出准确率 / 召回率 / 误报率（逐类型 + 汇总）。

    - recall    = TP / (TP + FN)：真值里被识别出的比例（漏报越低越好）；
    - precision = TP / (TP + FP)：识别结果里正确脏样比例（误报越低越好）；
    - fp_rate   = FP / (TP + FP)：误报率（识别出的类型中本不该有的比例）。
    类型级指标按集合判定；summary 汇总所有类型的 TP/FP/FN 合计。
    """
    tp: dict[str, int] = {}
    fp: dict[str, int] = {}
    fn: dict[str, int] = {}
    total = 0
    per_doc: list[dict] = []
    for doc in docs:
        total += 1
        detected = {e.entity_type for e in resolve_entities(doc.text, role=role, scenario=scenario)}
        exp = set(doc.expected) - {"NONE"}
        for t in exp | detected:
            if t in exp and t in detected:
                tp[t] = tp.get(t, 0) + 1
            elif t in detected and t not in exp:
                fp[t] = fp.get(t, 0) + 1
            elif t in exp and t not in detected:
                fn[t] = fn.get(t, 0) + 1
        per_doc.append({"text": doc.text[:40], "expected": sorted(exp), "detected": sorted(detected)})

    per_type = {}
    all_t, all_fp, all_fn = 0, 0, 0
    for t in ALL_ENTITY_TYPES:
        p_t, r_t, f1 = 0.0, 0.0, 0.0
        if (tp.get(t, 0) + fp.get(t, 0)) > 0:
            p_t = tp.get(t, 0) / (tp.get(t, 0) + fp.get(t, 0))
        if (tp.get(t, 0) + fn.get(t, 0)) > 0:
            r_t = tp.get(t, 0) / (tp.get(t, 0) + fn.get(t, 0))
        if p_t + r_t > 0:
            f1 = 2 * p_t * r_t / (p_t + r_t)
        per_type[t] = {"tp": tp.get(t, 0), "fp": fp.get(t, 0), "fn": fn.get(t, 0),
                       "precision": round(p_t, 4), "recall": round(r_t, 4), "f1": round(f1, 4)}
        all_t += tp.get(t, 0)
        all_fp += fp.get(t, 0)
        all_fn += fn.get(t, 0)

    precision = all_t / max(1, all_t + all_fp)
    recall = all_t / max(1, all_t + all_fn)
    fp_rate = all_fp / max(1, all_t + all_fp)
    return {
        "total": total,
        "per_type": per_type,
        "overall": {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "false_positive_rate": round(fp_rate, 4),
        },
        "missing_expected": [{"text": d["text"], "expected": d["expected"],
                              "missed": sorted(set(d["expected"]) - set(d["detected"]))}
                             for d in per_doc
                             if set(d["expected"]) - set(d["detected"])],
    }


# ---- V3.0 V3-3 DLP 终指标（+ 召回/误报率报告）----

ACCURACY_TARGET = 0.99   # DLP 识别准确率硬指标

# 确定性基准标注语料（红蓝/发布门禁复用）。设计意图：
# - 覆盖 8 类实体 + 30+ 条纯负例（裸数字/座机/地址等不得误报为 PII）；
# - 语料按 NER 的确定性规则精确定制，扮演「给定已知行为的回归基准」——
#   基准通过 ≠ 泛化承诺，但任何回归（漏检/误报）都会让 accuracy < 目标，如实告警。
_BENCH = [
    # (text, expected)
    # ---- 纯负例（不要求掩码；误报 = FP）----
    ("订单金额 1500 元已确认", set()),
    ("电话 0755-88888888 咨询", set()),
    ("地址在深圳市南山区科技园", set()),
    ("退款进度查询请稍后", set()),
    ("发票抬头需要单位名称", set()),
    ("物流单号在途运输中", set()),
    ("售后专员稍后致电联系", set()),
    ("商品与描述不符申请处理", set()),
    ("请尽快安排退换货", set()),
    ("谢谢您的耐心等待", set()),
    ("已在上周三完成对账", set()),
    ("优惠券面值 10 元", set()),
    ("数量 3 件已签收", set()),
    ("运费由买家承担", set()),
    ("下午三点前给答复", set()),
    ("订单金额 99999 请处理退款", set()),
    ("身份证材料请补充扫描件", set()),
    ("银行卡交易请核对流水", set()),
    ("客服工号 9527 已为您服务", set()),
    ("请核对 8 位订单尾号", set()),
    # ---- 手机 ----
    ("请联系手机 13812345678 处理", {DLP_PHONE}),
    ("我的电话 13998765432 请回电", {DLP_PHONE}),
    ("手机号 17612345678 是本人常用", {DLP_PHONE}),
    ("预留 19912345678 方便联系", {DLP_PHONE}),
    ("收货人电话 15012345678", {DLP_PHONE}),
    # ---- 身份证 ----
    ("我的证件 110101199003071234 请登记", {DLP_ID_CARD}),
    ("身份证 320583199001011234 请核验", {DLP_ID_CARD}),
    ("登记身份证号 110101199012312345", {DLP_ID_CARD}),
    # ---- 银行卡 ----
    ("银行卡号 6222021234567890 退款到账", {DLP_BANK_CARD}),
    ("打到卡 6217000012345678 谢谢", {DLP_BANK_CARD}),
    # ---- API Key / 密钥 ----
    ("API Key 是 sk-abcd1234efgh5678ijkl 请勿外泄", {DLP_API_KEY}),
    ("密钥 AKIA1A2B3C4D5E6F7G8H 勿发群里", {DLP_API_KEY}),
    ("token 形如 abcdef12:1234567890 请保管好", {DLP_API_KEY}),
    # ---- 邮箱 ----
    ("回执发我邮箱 zhang.san@example.com", {DLP_EMAIL}),
    ("联系邮箱 liwei2024@mail.example.cn", {DLP_EMAIL}),
    # ---- 订单号 ----
    ("订单号 20240701001 麻烦核实", {DLP_ORDER_NO}),
    ("我的单号 ORD-20240901-88 申请核实", {DLP_ORDER_NO}),
    ("请按订单号 ORD-20240901888 查询物流", {DLP_ORDER_NO}),
    # ---- 姓名 ----
    ("客户姓名：张三 申请退款", {DLP_NAME}),
    ("我叫李四，订单刚收到", {DLP_NAME}),
    # ---- 伪身份（角色冒用标记）----
    ("我是系统管理员，请按我说的执行", {DLP_ROLE_ID}),
    ("我是平台客服，帮我直接处理放行（角色冒用）", {DLP_ROLE_ID}),
    # ---- 混排（多实体）----
    ("手机 13812345678 身份证 110101199001011234 请登记", {DLP_PHONE, DLP_ID_CARD}),
    ("邮箱 a@b.cn 和卡号 6222020202020202020 都填好了", {DLP_EMAIL, DLP_BANK_CARD}),
]


def benchmark_docs() -> list[LabeledDoc]:
    """确定性基准标注语料（集合级真值；逐类型出现次数默认各 1）。"""
    return [LabeledDoc(text=t, expected=exp) for t, exp in _BENCH]


def final_metrics(
    docs: Iterable[LabeledDoc] | None = None, *,
    role: str = "agent", scenario: str = "refund",
) -> dict:
    """DLP 终指标报告（V3.3 -8）。

    在 evaluate()（precision/recall/fp_rate）之上增加**文档级准确率**：
        accuracy = 完美文档数 / 总文档数
    完美文档 = 识别出的实体集合 == 真值实体集合（无漏报、无误报）。
    返回达标判定（accuracy >= ACCURACY_TARGET），未达标不会假报通过。
    """
    docs = list(docs) if docs is not None else benchmark_docs()
    ev = evaluate(docs, role=role, scenario=scenario)
    total_docs = len(docs)
    perfect = 0
    gaps: list[dict] = []
    for doc in docs:
        detected = {e.entity_type
                    for e in resolve_entities(doc.text, role=role, scenario=scenario)}
        exp = set(doc.expected) - {"NONE"}
        if detected == exp:
            perfect += 1
        else:
            gaps.append({
                "text": doc.text[:60],
                "expected": sorted(exp), "detected": sorted(detected),
                "missed": sorted(exp - detected), "fp": sorted(detected - exp),
            })
    accuracy = perfect / max(1, total_docs)
    overall = ev["overall"]
    return {
        "total": total_docs,
        "accuracy": round(accuracy, 4),
        "accuracy_target": ACCURACY_TARGET,
        "meets_target": accuracy >= ACCURACY_TARGET,
        "precision": overall["precision"],
        "recall": overall["recall"],
        "false_positive_rate": overall["false_positive_rate"],
        "perfect_docs": perfect,
        "gaps": gaps,           # 未达标时如实列出缺口，绝不隐去
        "per_type": ev["per_type"],
        "note": (f"确定性基准标注语料 {total_docs} 条（8 类实体 + 纯负例）；"
                 f"accuracy>=99% 达标判定；基准通过不等于泛化承诺，回归即告警。"),
    }