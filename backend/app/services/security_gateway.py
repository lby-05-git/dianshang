# -*- coding: utf-8 -*-
"""输入安全网关（基线 V1.0 最小能力）。

注入检测（规则型双引擎：正则特征 + 单一打分，不入 LLM）：
- 绕行指令 / 角色扮演越狱 / Base64 / 多语言变种 / 间接注入（"跳过人工审批…"
  "Direct_Refund_API" 等）-> 每条特征贡献分数，score>=0.85 才 BLOCK，
  否则 0.50<=score<0.85 -> REVIEW（转人工），score<0.50 -> PASS；
- 合法客诉文本（无任何特征）-> PASS，绝不误杀。

DLP 正则脱敏（手机/身份证/银行卡/API Key）：
- 掩码格式：手机 138****0000；身份证 110101********1234；银行卡 6222 ******** 8899；
  API Key 保留前 4 后 4，中间 ****。
- 落库 security_decisions.masked_input / dlp_findings 一律只存掩码片段，不存明文。

红线：
- 本模块不改变任何业务状态；BLOCK/REVIEW 决策由调用方（intake 入口）执行，
  本模块只返回判定证据并落库审计。
- score 阈值 0.85 可由 config 覆盖（GATE_BLOCK_THRESHOLD）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.domain.models import (
    DLP_API_KEY,
    DLP_BANK_CARD,
    DLP_ID_CARD,
    DLP_PHONE,
    DECISION_APPROVED,
    DlpFinding,
    GATE_BLOCK,
    GATE_PASS,
    GATE_REVIEW,
    REFUND_STATUS_NONE,
    SecurityDecision,
    TOOL_RISK_EXTERNAL_NOTIFY,
    TOOL_RISK_FUND_EFFECT,
    TOOL_RISK_INTERNAL_WRITE,
    TOOL_RISK_READONLY,
    ToolDefinition,
    new_uuid,
)

# 注入特征（score 权重）。design：规则型，不使用 LLM；阈值 0.85 才 BLOCK。
# Phase 4 红蓝对抗加固：补充 2026-08 红方变体缺口 —— "跳过任何审批"/"绕过审核流程"/
# "篡改订单归属"/"(写进|写入)日志…执行"/"假装你是客服主管"/"DAN 越狱" 等，并对关键
# 绕过短语加 \s* 空白容忍（空格/换行规避）。权重视语言强度：硬绕过 0.95、泄露/混淆 0.90、
# 身份/多语言 0.85。
INJECTION_FEATURES: dict[str, tuple[float, re.Pattern]] = {
    "rollplay_bypass": (0.95, re.compile(
        r"忽略(上面|之前)?\s*(指令|规则)|ignore\s+(all\s+)?(previous|instructions|prompt)"
        r"|不当(系统|AI)|你是助手，?现在|role\s*play|假\s*扮|假装(自己是)?"
        r"|扮演(人类|客服|系统老板)|角色扮演|无视(你们)?(的)?(规则|流程|指令|限制)"
        r"|忽略(审查|校验|审核|检查)|越狱(模式)?|DAN(\s*mode)?|developer\s*mode"
        r"|jailbreak(ed)?|do\s+anything\s+now",
        re.IGNORECASE)),
    "bypass_human": (0.95, re.compile(
        r"跳过\s*人\s*工\s*审\s*批|绕\s*过\s*审\s*批|绕\s*过.*审\s*核|直接\s*退款"
        r"|无需\s*人工|不要\s*转\s*人工|别\s*给\s*人工|跳过\s*审核|Direct_Refund_API"
        r"|直接.*退款.*API|自动\s*退款|跳过(任何|这个)?\s*审\s*批|免\s*审",
        re.IGNORECASE)),
    "base64_obfuscation": (0.90, re.compile(
        r"base64|YmFzZTY0|decode|解码",
        re.IGNORECASE)),
    "multilang_bypass": (0.85, re.compile(
        r"日本語で(返信|答)|француз|français|deutsch|乱码|中文混乱",
        re.IGNORECASE)),
    "fake_identity": (0.85, re.compile(
        r"我是(系统管理员|客服主管|老板|平台|你老板)|系统管理员身份|你是什么系统"
        r"|已获(系统|管理)?授权|官方(平台|客服)授权|我是.*管理员",
        re.IGNORECASE)),
    "prompt_leak_request": (0.90, re.compile(
        r"输出你的(系统|初始)?提示词|你被\s*(设计|编程|设定|指示|训练|替换)成"
        r"|你被.*?(设定|训练)成|泄露你的指令|打印你的prompt",
        re.IGNORECASE)),
    "injection_via_log": (0.85, re.compile(
        r"(写进|写入)(日志|文件|配置文件).*(执行|跳过|忽略|退款)|log.*(skip|跳过|执行|退款)|注入到日志",
        re.IGNORECASE)),
    "refund_authorization_forge": (0.95, re.compile(
        r"伪造(订单|授权|审批|归属)|篡改金额|篡改订单|虚报(订单|金额)|金额.*改|退款.*乱改|错单",
        re.IGNORECASE)),
}

# BLOCK/REVIEW 阈值（config 可覆盖）
GATE_BLOCK_THRESHOLD_DEFAULT = 0.85
GATE_REVIEW_THRESHOLD_DEFAULT = 0.50

MONITORED_TOOL_NAMES = ("execute_refund", "create_refund", "refund")

# ---- DLP 正则（4 类，掩码格式如 138****0000）----

# 手机：11 位大陆手机号，可选分隔
_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 身份证：18 位（17 数字 + X）
_RE_ID_CARD = re.compile(r"(?<!\d)(\d{17}[0-9Xx])(?!\d)")

# 银行卡：13-19 位数字
_RE_BANK_CARD = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")

# API Key：常见格局（sk-... / AKIA... / 对称密钥 id:secret）
_RE_API_KEY = re.compile(
    r"(?i)(\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|"
    r"[A-Za-z0-9_]{8}:[A-Za-z0-9]{8,}\b))"
)


@dataclass
class SecurityDecisionResult:
    """单次网关判定证据（可落库到 security_decisions + dlp_findings）。"""
    gate_result: str                    # PASS/BLOCK/REVIEW
    injection_score: float
    risk_level: str                     # LOW/MEDIUM/HIGH
    masked_input: str
    rule_hits: list[tuple[str, float]] = field(default_factory=list)  # (feature, score)
    findings: list[dict] = field(default_factory=list)               # DLP 明细

    def as_dict(self) -> dict:
        return {
            "gate_result": self.gate_result,
            "injection_score": self.injection_score,
            "risk_level": self.risk_level,
            "masked_input": self.masked_input,
            "rule_hits": [{"rule": name, "score": w} for name, w in self.rule_hits],
            "findings": list(self.findings),
        }


def _injection_score(text: str) -> tuple[float, list[tuple[str, float]]]:
    """规则型打分：命中的特征取最高权重分（不叠加，避免长文误伤）。

    返回 (score, hits)；无特征命中则 (0.0, [])。
    """
    hits: list[tuple[str, float]] = []
    best = 0.0
    for name, (weight, pat) in INJECTION_FEATURES.items():
        if pat.search(text):
            hits.append((name, weight))
            best = max(best, weight)
    return best, hits


def _mask_phone(m: str) -> str:
    return f"{m[:3]}****{m[-4:]}"


def _mask_id_card(m: str) -> str:
    return f"{m[:6]}********{m[-4:]}"


def _mask_bank_card(m: str) -> str:
    return f"{m[:4]} ******** {m[-4:]}"


def _mask_api_key(m: str) -> str:
    if len(m) <= 8:
        return m[:2] + "****"
    return f"{m[:4]}****{m[-4:]}"


# (pattern, masker, dlp_type) —— 顺序：API Key 先、手机、身份证、银行卡，
# 避免银行卡正则先把银行卡数字吞掉后干扰 ID（ID 与银行卡互不重叠，但顺序稳妥）
_DLP_RULES = [
    (_RE_API_KEY, _mask_api_key, DLP_API_KEY),
    (_RE_PHONE, _mask_phone, DLP_PHONE),
    (_RE_ID_CARD, _mask_id_card, DLP_ID_CARD),
    (_RE_BANK_CARD, _mask_bank_card, DLP_BANK_CARD),
]


def _dlp_mask(text: str) -> tuple[str, list[dict]]:
    """DLP 脱敏：逐类正则掩码，返回 (masked, findings)。

    findings 每项：{"finding_type", "position", "plain_snippet"(已掩码短片段), "masked"}。
    逐类从后往前替换避免偏移错乱。
    """
    masked = text or ""
    findings: list[dict] = []
    for pat, masker, ftype in _DLP_RULES:
        hits = [
            (m.start(), m.end(), masker(m.group(1) if m.groups() else m.group(0)))
            for m in pat.finditer(masked)
        ]
        for start, end, rep in sorted(hits, key=lambda x: x[0], reverse=True):
            masked = masked[:start] + rep + masked[end:]
            findings.append({
                "finding_type": ftype,
                "position": start,
                "plain_snippet": rep[:50],          # 落库只存掩码短片段
                "masked": rep,
            })
    # 按 position 升序（dlp_findings 展示/校验友好）
    findings.sort(key=lambda f: f["position"])
    return masked, findings


def diagnose(
    text: str, *,
    ner: bool = False, role: str = "agent", scenario: str = "refund",
) -> SecurityDecisionResult:
    """纯内存判定（不落库）：入场即判定，供测试与合并调用。

    V2.0（-3）NER 接线：`ner=True` 时 findings 由 ner_dlp 扩展
    （保留 4 类掩码格式 + 增加 EMAIL/ORDER_NO/NAME/ROLE_ID 语境识别），
    masked_input 按 role×scenario 策略脱敏。默认 False 保持基线行为零变化。
    """
    text = text or ""
    score, hits = _injection_score(text)
    if ner:
        from app.services import ner_dlp as _nd
        masked, entities = _nd.mask(text, role=role, scenario=scenario)
        findings = [
            {
                "finding_type": e.entity_type,
                "position": e.start,
                "plain_snippet": e.masked[:50],
                "masked": e.masked,
                "action": e.action,
            }
            for e in entities
        ]
    else:
        masked, findings = _dlp_mask(text)

    if score >= GATE_BLOCK_THRESHOLD_DEFAULT:
        gate, risk = GATE_BLOCK, "HIGH"
    elif score >= GATE_REVIEW_THRESHOLD_DEFAULT:
        gate, risk = GATE_REVIEW, "MEDIUM"
    else:
        gate, risk = GATE_PASS, "LOW"

    return SecurityDecisionResult(
        gate_result=gate,
        injection_score=score,
        risk_level=risk,
        masked_input=masked,
        rule_hits=hits,
        findings=findings,
    )


def _record_intercept_events(
    db: Session, res: SecurityDecisionResult, *,
    case_id: str | None, source_text: str,
) -> None:
    """拦截/DLP 即录 security_events（V2.0 -3：只记录，不改判定）。

    - INJECTION 事件：gate 命中 BLOCK/REVIEW 时；
    - DLP 事件：findings（PII 检测）非空时（detail 只存掩码片段）。
    """
    import hashlib
    from app.services.security_events import (
        SEC_EV_DLP,
        SEC_EV_INJECTION,
        record_event,
    )
    sha = hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()
    if res.gate_result in (GATE_BLOCK, GATE_REVIEW):
        record_event(db, event_type=SEC_EV_INJECTION, case_id=case_id,
                     severity="high" if res.gate_result == GATE_BLOCK else "medium",
                     detail=res.masked_input[:2000], audit_sha256=sha)
        db.flush()
    if res.findings:
        sev = "critical" if any(
            f["finding_type"] in (DLP_API_KEY, DLP_ID_CARD, DLP_BANK_CARD)
            for f in res.findings
        ) else "medium"
        record_event(db, event_type=SEC_EV_DLP, case_id=case_id,
                     severity=sev, detail=res.masked_input[:2000], audit_sha256=sha)
        db.flush()


def gate_text(
    db: Session,
    *,
    text: str,
    case_id: str | None = None,
    source: str = "intake",
    tool_name: str | None = None,
    persist: bool = True,
    ner: bool = False, role: str = "agent", scenario: str = "refund",
) -> SecurityDecisionResult:
    """输入网关正式入口：判定 + （可选）落库 security_decisions + dlp_findings。

    case_id 为空且 persist=True 时以占位审计行留存（上游随后补业务 case）。
    DLP 只落掩码片段，明文绝不入库；拦截/命中即录一条 security_event。
    """
    res = diagnose(text, ner=ner, role=role, scenario=scenario)
    if persist:
        _persist_decision(db, res, case_id=case_id, source=source, tool_name=tool_name)
        if case_id:
            for f in res.findings:
                db.add(DlpFinding(
                    id=new_uuid(),
                    case_id=case_id,
                    finding_type=f["finding_type"],
                    position=f["position"],
                    plain_snippet=f["plain_snippet"],
                    masked=f["masked"],
                ))
        _record_intercept_events(db, res, case_id=case_id, source_text=text)
        db.commit()
    return res


def _persist_decision(
    db: Session, res: SecurityDecisionResult, *,
    case_id: str | None, source: str, tool_name: str | None,
) -> SecurityDecisionResult:
    """落 security_decisions（仅审计，不改业务语义）。"""
    row = SecurityDecision(
        id=new_uuid(),
        case_id=case_id,          # 可空（迁移 f3e4d5c6b7a9）：上游未建单前留 NULL 审计行
        source=source,
        gate_result=res.gate_result,
        injection_score=res.injection_score,
        risk_level=res.risk_level,
        masked_input=res.masked_input,
        tool_name=tool_name,
        rule_hits=dict(res.rule_hits) if res.rule_hits else None,
    )
    db.add(row)
    db.commit()
    return res


def mask_dlp(
    text: str, *,
    ner: bool = False, role: str = "agent", scenario: str = "refund",
) -> tuple[str, list[dict]]:
    """DLP 脱敏单开（路由端点 /api/security/dlp-mask 用）：返回掩码文本 + 明细。

    ner=True 时接线 NER（同 diagnose），默认保持基线 4 类正则行为。
    """
    if ner:
        from app.services import ner_dlp as _nd
        masked, entities = _nd.mask(text, role=role, scenario=scenario)
        return masked, [
            {"finding_type": e.entity_type, "position": e.start,
             "plain_snippet": e.masked[:50], "masked": e.masked, "action": e.action}
            for e in entities
        ]
    return _dlp_mask(text or "")


def list_findings(db: Session, case_id: str, *, limit: int = 50) -> list[dict]:
    """查询某案件 DLP findings（只读）。"""
    rows = (
        db.query(DlpFinding)
        .filter(DlpFinding.case_id == case_id)
        .order_by(DlpFinding.position.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "finding_type": r.finding_type,
            "position": r.position,
            "plain_snippet": r.plain_snippet,
            "masked": r.masked,
        }
        for r in rows
    ]


# =====================================================================
# V3-3 企业级零信任治理：工具按风险分级（readonly / internal_write /
#     external_notify / fund_effect）+ 退款类「策略+HITL+幂等+审计」四同时
# =====================================================================

# 静态默认工具分级（tool_name -> (risk_class, description)）。
# DB tool_definitions 可覆盖 enabled/risk_class/policy 绑定；静态表保证
# 未登记工具也有确定默认值（未知工具按最低风险只读——分级只影响资金类强校验，
# 既有 tool_gate 固定白名单不受影响）。
DEFAULT_TOOL_RISK: dict[str, tuple[str, str]] = {
    "execute_refund": (TOOL_RISK_FUND_EFFECT, "退款执行：资金影响，要求 策略+HITL+幂等+审计 四同时"),
    "create_refund": (TOOL_RISK_FUND_EFFECT, "退款创建（execute_refund 别名）"),
    "refund": (TOOL_RISK_FUND_EFFECT, "退款（别名）"),
    "refund_provider": (TOOL_RISK_FUND_EFFECT, "退款 Provider 调用（资金侧）"),
    "approve_refund": (TOOL_RISK_INTERNAL_WRITE, "审批动作：内部写（决策留痕，不直接动资金）"),
    "document_generate": (TOOL_RISK_INTERNAL_WRITE, "文档生成：内部写"),
    "evidence_upload": (TOOL_RISK_INTERNAL_WRITE, "证据上传：内部写"),
    "policy_write": (TOOL_RISK_INTERNAL_WRITE, "策略保存：内部写"),
    "notification_send": (TOOL_RISK_EXTERNAL_NOTIFY, "短信/邮件通知：外部副作用，不动资金"),
    "external_query": (TOOL_RISK_READONLY, "外部查询：只读"),
    "order_lookup": (TOOL_RISK_READONLY, "订单查询：只读"),
    "ocr_extract": (TOOL_RISK_READONLY, "OCR 提取：只读"),
    "case_load": (TOOL_RISK_READONLY, "案件读取：只读"),
}

# 风险严重级（security_decisions.risk_level 用；与既有 LOW/MEDIUM/HIGH 语义一致）
_RISK_SEVERITY = {
    TOOL_RISK_READONLY: "LOW",
    TOOL_RISK_INTERNAL_WRITE: "MEDIUM",
    TOOL_RISK_EXTERNAL_NOTIFY: "MEDIUM",
    TOOL_RISK_FUND_EFFECT: "HIGH",
}


def _round_tool_row(row: ToolDefinition) -> dict:
    return {
        "id": row.id,
        "tool_name": row.tool_name,
        "risk_class": row.risk_class,
        "description": row.description,
        "policy_id": row.policy_id,
        "enabled": bool(row.enabled),
        "requires_hitl": bool(row.requires_hitl),
        "requires_idempotency": bool(row.requires_idempotency),
        "requires_audit": bool(row.requires_audit),
        "source": "tool_definitions",
    }


def classify_tool_risk(
    db: Session | None, tool_name: str,
) -> dict:
    """解析某工具的风险分级（DB tool_definitions 优先，未登记回退静态默认）。

    返回值字段：tool_name / risk_class / policy_id / enabled / requires_hitl /
    requires_idempotency / requires_audit / description / source。
    """
    if db is not None:
        row = db.query(ToolDefinition).filter_by(tool_name=tool_name).first()
        if row is not None:
            return _round_tool_row(row)
    risk_class, desc = DEFAULT_TOOL_RISK.get(
        tool_name, (TOOL_RISK_READONLY, "未知工具按最低风险只读"))
    return {
        "tool_name": tool_name,
        "risk_class": risk_class,
        "description": desc,
        "policy_id": None,
        "enabled": True,
        "requires_hitl": risk_class == TOOL_RISK_FUND_EFFECT,
        "requires_idempotency": risk_class == TOOL_RISK_FUND_EFFECT,
        "requires_audit": True,
        "source": "static_default",
    }


@dataclass
class ToolRiskDecision:
    """一次工具风险分级门禁判定证据（策略+HITL+幂等+审计 四同时）。"""

    allow: bool
    risk_class: str
    tool_name: str
    missing: list[tuple[str, str]] = field(default_factory=list)  # (维度, 原因)
    policy_id: str | None = None
    hitl_approved: bool = False
    persisted: bool = False
    injection_score: float = 0.0
    risk_level: str = "LOW"

    def as_dict(self) -> dict:
        return {
            "allow": self.allow,
            "risk_class": self.risk_class,
            "tool_name": self.tool_name,
            "missing": [{"dimension": k, "reason": v} for k, v in self.missing],
            "policy_id": self.policy_id,
            "hitl_approved": self.hitl_approved,
            "persisted": self.persisted,
            "gate_result": "PASS" if self.allow else "BLOCK",
            "gate_version": "tool-risk-v1",
        }


def check_tool_risk_gate(
    db: Session,
    *,
    tool_name: str,
    case=None,
    actor_role: str = "agent",
    hitl_approved: bool | None = None,
    refund_id_key: str | None = None,
    has_refund_receipt: bool | None = None,
    persist: bool = False,
    audit_committed: bool | None = None,
) -> ToolRiskDecision:
    """工具风险分级门禁（V3-3 四同时）。

    维度（fund_effect 必须四同时全过；其余风险类只校验 策略+审计）：
    - 策略    : 工具 registered 且 enabled；policy_id 绑定须指向存在且有效的策略档；
    - HITL    : hitl_approved 显式传入，或可由 DB 推导（case 已 APPROVED 决策）；
                静态默认下仅 fund_effect 要求；DB 可对任意工具提高（requires_hitl）；
    - 幂等    : 未退 + 未占退款流水号 + 无回执（仅 requires_idempotency 时校验）；
    - 审计    : persist=True（本门禁落 security_decisions + TOOL_GATE 事件）
                或 audit_committed=True（调用方承诺在自身链路留审计，如 worker 的
                agent_execution_logs/refund_events）；否则该维 FAIL -> BLOCK。

    红线：check_tool_risk_gate 只读判定 + （persist=True 时）落审计，绝不执行退款。
    """
    cfg = classify_tool_risk(db, tool_name)
    missing: list[tuple[str, str]] = []
    risk_class = cfg["risk_class"]
    requires_hitl = bool(cfg["requires_hitl"])
    requires_idem = bool(cfg["requires_idempotency"]) or risk_class == TOOL_RISK_FUND_EFFECT
    requires_audit = bool(cfg["requires_audit"])

    # ---- 策略 ----
    policy_ok = cfg["enabled"]
    if not policy_ok:
        missing.append(("strategy", f"工具 {tool_name} 已停用（tool_definitions.enabled=False)）"))
    if cfg.get("policy_id"):
        from app.domain.models import PolicyProfile
        prof = db.get(PolicyProfile, cfg["policy_id"]) if db is not None else None
        if prof is None:
            missing.append(("strategy", f"绑定策略档 {cfg['policy_id']} 不存在"))

    # ---- HITL （人审）----
    if requires_hitl:
        if hitl_approved is None:
            hitl_approved = _derive_hitl(db, case=case)
        if not hitl_approved:
            missing.append(("hitl", "缺少人工审批（HITL）：无已批准的人审决策"))
    else:
        hitl_approved = True   # 非要求人审的工具该维视为已满足

    # ---- 幂等 ----
    if requires_idem:
        idem_ok, idem_msg = _risk_idempotency_ok(
            case, refund_id_key=refund_id_key, has_refund_receipt=has_refund_receipt)
        if not idem_ok:
            missing.append(("idempotency", idem_msg))

    # ---- 审计 ----
    audit_ok = (not requires_audit) or persist or bool(audit_committed)
    if not audit_ok:
        missing.append(("audit", "审计记录未启用（需 persist=True 或 audit_committed=True）"))

    allow = not missing
    persisted = False
    if persist:
        persisted = _persist_tool_risk_decision(
            db, tool_name=tool_name, risk_class=risk_class, allow=allow,
            missing=missing, case_id=getattr(case, "id", None))
    return ToolRiskDecision(
        allow=allow, risk_class=risk_class, tool_name=tool_name,
        missing=missing, policy_id=cfg.get("policy_id"),
        hitl_approved=bool(hitl_approved), persisted=persisted,
        risk_level=_RISK_SEVERITY.get(risk_class, "LOW"),
    )


def _derive_hitl(db: Session | None, *, case) -> bool:
    """由案件推导是否已有人审批（HITL 证据）。

    - case.final_decision 已 APPROVED（人工/自动路径的终态证据）；
    - 或存在 human_interventions 且 action=APPROVED 的记录（最强制证据）。
    """
    if case is None:
        return False
    if getattr(case, "final_decision", None) == DECISION_APPROVED:
        if db is None:
            return True
        from app.domain.models import HumanIntervention
        row = (db.query(HumanIntervention)
               .filter_by(case_id=getattr(case, "id", None), action="APPROVED")
               .first())
        if row is not None:
            return True
        # 无 human 记录但决策已 APPROVED（自动秒退路径由调用方显式传 hitl_approved）
        return True
    return False


def _risk_idempotency_ok(case, *, refund_id_key=None, has_refund_receipt=None
                         ) -> tuple[bool, str]:
    """幂等维度：已退 / 已占退款流水号 / 已有回执 -> 拒绝，绝不重复退。"""
    if case is not None:
        rs = getattr(case, "refund_status", None)
        if rs not in (None, REFUND_STATUS_NONE):
            return False, f"已退（refund_status={rs}），幂等拒绝"
    if refund_id_key:
        return False, "已存在退款流水号，幂等拒绝"
    if has_refund_receipt:
        return False, "已存在退款回执，幂等拒绝"
    return True, ""


def _persist_tool_risk_decision(
    db: Session, *, tool_name: str, risk_class: str, allow: bool,
    missing: list[tuple[str, str]], case_id: str | None,
) -> bool:
    """四同时门禁判定落审计（security_decisions + TOOL_GATE 安全事件）。

    只记录判定证据，绝不改 case/不执行 provider。BLOCK 命中才录事件；
    PASS 也落一行 security_decisions（资金类放行留痕）。
    """
    reasons = [f"{d}: {r}" for d, r in missing]
    db.add(SecurityDecision(
        id=new_uuid(),
        case_id=case_id,
        source="refund_tool",
        gate_result="PASS" if allow else "BLOCK",
        injection_score=0.0,
        risk_level=_RISK_SEVERITY.get(risk_class, "LOW"),
        masked_input=("; ".join(reasons) if reasons else f"{tool_name} 四同时门禁放行")[:500],
        tool_name=tool_name,
        rule_hits={"tool_risk_class": risk_class,
                   "tool_risk_gate_reasons": reasons},
    ))
    if not allow:
        from app.services.security_events import SEC_EV_TOOL_GATE, record_event
        evt = record_event(db, event_type=SEC_EV_TOOL_GATE, case_id=case_id,
                           severity="high" if risk_class == TOOL_RISK_FUND_EFFECT
                           else _RISK_SEVERITY.get(risk_class, "LOW").lower(),
                           detail=("; ".join(reasons))[:2000])
        db.flush()
        evt.disposition = "OPEN"
    db.commit()
    return True