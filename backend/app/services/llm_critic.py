# -*- coding: utf-8 -*-
"""Critic 升级：规则（security_gateway）+ 轻量语义模型 → 风险分 + 可追溯原因（V2.0 -3）。

- 规则轨：security_gateway.diagnose —— 8 类注入特征（无 LLM），BLOCK>=0.85 / REVIEW>=0.50；
- 语义轨：确定性「轻量语义模型」——攻击意图原型（锚点 bigram/词袋）× 文本 token 命中率，
  捕捉正则难以穷举的同义/变体措辞；产出语义风险分 + 命中原型 + 锚点清单，供可追溯。
- 合并规则（V-R3 缓解：语义高置信才 BLOCK，否则 REVIEW 转人工）：
    BLOCK  只有两种来源：
      (a) 规则分 >= 0.85（高置信规则直判）；
      (b) 规则分 >= 0.50 且 语义分 >= SEM_BLOCK_JOINT（规则+语义双轨互证）。
    REVIEW 来源：规则 0.50~0.85；或 语义分 >= SEM_REVIEW_ALONE 且锚点命中 >= 2（语义高置信 → 转人工）。
    其余 PASS。
  ⇒ 语义轨道只会「增加拦截 / 升级 REVIEW→BLOCK」，绝不放过既有规则拦截 ——
    red_blue 注入/越狱拦截率只会保持或上升（红蓝辨应）。
- 可追溯：verdict.reasons —— 规则命中（RULE:<name>）与语义命中（SEM:<proto>:<score><锚点>）。
- DLP：masked_input / findings 由 ner_dlp 生成（role/scenario 策略脱敏），掩码后明文绝不含。
- 拦截即录：persist 且 verdict != PASS → 落 security_decisions(+可选 security_events INJECTION/DLP)。
  只记录证据与事件，绝不修改任何判定/退款/业务状态。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.domain.models import (
    DLP_API_KEY,
    DLP_BANK_CARD,
    DLP_ID_CARD,
    GATE_BLOCK,
    GATE_PASS,
    GATE_REVIEW,
)
from app.services import ner_dlp
from app.services.security_gateway import (
    GATE_BLOCK_THRESHOLD_DEFAULT,
    GATE_REVIEW_THRESHOLD_DEFAULT,
)
from app.services.security_events import SEC_EV_DLP, SEC_EV_INJECTION, record_event

# 语义阈值（调参见测试 test_llm_critic.py：合法不误杀 & 攻击变体皆命中）
SEM_BLOCK_JOINT = 0.62      # 规则(>=0.50) & 语义 >= 此值 → BLOCK（双轨互证）
SEM_REVIEW_ALONE = 0.55     # 语义单独 >= 此值 且 锚点 >= 2 → REVIEW（转人工）
SEM_SCALE = 0.55            # weighted → 语义分映射系数
MIN_ANCHOR_FOR_SEM_REVIEW = 2


# =====================================================================
# 轻量语义模型：攻击意图原型（锚点 = 强风险措辞；aux = 场景词）
# =====================================================================

@dataclass(frozen=True)
class _Proto:
    key: str
    label: str
    anchors: tuple[str, ...]
    aux: tuple[str, ...] = ()


_SEMANTIC_PROTOTYPES: tuple[_Proto, ...] = (
    _Proto("authority_bypass", "权利绕过/免审",
           ("跳过", "绕过", "无视", "忽略", "免除", "免审", "免批", "秒退", "秒到",
            "走后台", "开后门", "后门", "特批", "免检", "直接过", "省去", "无须审",
            "不用审", "无需审", "快速通道", "绿色通道", "特事特办"),
           ("审批", "人工", "审核", "流程", "规则", "指令", "限制", "自动", "直接",
            "再审", "放行", "不再", "权利", "即退", "立刻", "马上")),
    _Proto("identity_spoof", "角色冒用/伪身份",
           ("扮演", "假装", "假扮", "冒充", "假称", "伪装成", "你不受限", "无限制"),
           ("客户", "主管", "老板", "管理员", "平台", "创始人", "系统", "身份", "经理")),
    _Proto("prompt_leak", "提示词泄露",
           ("泄露", "泄漏", "提示词", "系统指令", "系统提示", "打印", "原样", "设计成",
            "设定成", "训练成", "你被"),  # 提示词/你被 为双词歧义：搭配其它锚点才算语义命中
           ("指令", "prompt", "输出", "内容", "源码")),
    _Proto("forgery_abuse", "伪造/篡改",
           ("伪造", "篡改", "虚报", "虚构", "冒充", "假订单", "假流水", "假收据",
            "改金额", "改归属", "改状态"),
           ("订单", "金额", "归属权名", "流水", "错单", "退款", "99")),
    _Proto("log_injection", "日志间接注入",
           ("写进", "写入", "注入", "日志", "log", "存日志"),
           ("执行", "跳过", "退款", "file", "config", "echo")),
    _Proto("obfuscation_bypass", "混淆/多语言绕过",
           ("base64", "解码", "编码", "decode", "encode", "乱码", "伪装",
            "日文", "法语", "德文"),
           ("绕过", "混淆", "还原", "translate")),
)


def _tokenize(text: str) -> set[str]:
    """确定性 token 化：CJK 双字滑动窗口 + 拉丁词小写 + 模板标记。

    双字窗口保证「跳过」「审批」等 2 字锚点稳定匹配，同时容忍换行/空格插入。
    """
    toks: set[str] = set()
    for seq in re.findall(r"[一-龥]+", text or ""):
        if len(seq) >= 2:
            for i in range(len(seq) - 1):
                toks.add(seq[i:i + 2])
    for w in re.findall(r"[a-zA-Z0-9_]+", text or ""):
        toks.add(w.lower())
    if "{{" in (text or ""):
        toks.add("{{")
    if "}}" in (text or ""):
        toks.add("}}")
    return toks


def _semantic_score(text: str) -> dict:
    """轻量语义评分：返回 {score, proto, anchor_hits, anchors, aux_weight}。"""
    toks = _tokenize(text)
    best: dict | None = None
    for p in _SEMANTIC_PROTOTYPES:
        anchors = [a for a in p.anchors if a in toks]
        aux_hit = [a for a in p.aux if a in toks]
        weighted = len(anchors) + 0.3 * len(aux_hit)
        if weighted == 0:
            continue
        score = min(1.0, weighted * SEM_SCALE)
        cand = {
            "score": round(score, 4),
            "proto": p.key,
            "proto_label": p.label,
            "anchor_hits": len(anchors),
            "anchors": anchors[:6],
            "aux": aux_hit[:6],
        }
        if best is None or score > best["score"]:
            best = cand
    if best is None:
        return {"score": 0.0, "proto": None, "proto_label": None,
                "anchor_hits": 0, "anchors": [], "aux": []}
    return best


# =====================================================================
# 判定结果
# =====================================================================

@dataclass
class CriticVerdict:
    verdict: str                       # PASS / REVIEW / BLOCK
    risk_score: float
    risk_level: str                    # LOW / MEDIUM / HIGH
    reasons: list[str] = field(default_factory=list)
    rule_hits: list[tuple[str, float]] = field(default_factory=list)
    rule_score: float = 0.0
    semantic: dict = field(default_factory=dict)
    masked_input: str = ""
    findings: list[dict] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    decision_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "gate_result": self.verdict,          # 兼容网关字段
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
            "rule_hits": [{"rule": n, "score": w} for n, w in self.rule_hits],
            "semantic": dict(self.semantic),
            "masked_input": self.masked_input,
            "findings": list(self.findings),
            "event_ids": list(self.event_ids),
        }


def _combine(rule_score: float, rule_hits: list[tuple[str, float]],
             sem: dict) -> tuple[str, str, list[str]]:
    """合并规则分 + 语义分 → (verdict, risk_level, reasons)。"""
    reasons: list[str] = []
    verdict, level = GATE_REVIEW, "MEDIUM"

    for name, w in rule_hits:
        reasons.append(f"规则[{name}] 权重{w:.2f} 触发")
    if sem["proto"]:
        note = (f"语义[{sem['proto_label']}] 分{sem['score']:.2f} "
                f"锚点{sem['anchor_hits']}个:{','.join(sem['anchors'])}")
        if sem["score"] >= SEM_BLOCK_JOINT:
            note += "（规则+语义双轨互证）"
        reasons.append(note)

    if rule_score >= GATE_BLOCK_THRESHOLD_DEFAULT:
        verdict, level = GATE_BLOCK, "HIGH"
        reasons.append("规则分≥0.85 高置信阻断")
    elif rule_score >= GATE_REVIEW_THRESHOLD_DEFAULT and sem["score"] >= SEM_BLOCK_JOINT:
        verdict, level = GATE_BLOCK, "HIGH"
        reasons.append("规则分≥0.50 且语义分≥0.62：双轨证据阻断")
    elif rule_score >= GATE_REVIEW_THRESHOLD_DEFAULT:
        verdict, level = GATE_REVIEW, "MEDIUM"
        reasons.append("规则分 0.50~0.85：转人工复核")
    elif sem["score"] >= SEM_REVIEW_ALONE and sem["anchor_hits"] >= MIN_ANCHOR_FOR_SEM_REVIEW:
        verdict, level = GATE_REVIEW, "MEDIUM"
        reasons.append("语义高置信（新措辞），转人工复核")
    else:
        verdict, level = GATE_PASS, "LOW"
        reasons = []
    return verdict, level, reasons


def _dlp_severity(types: list[str]) -> str:
    from app.services.security_events import SEV_CRITICAL, SEV_HIGH
    if any(t in (DLP_API_KEY, DLP_ID_CARD, DLP_BANK_CARD) for t in types):
        return SEV_CRITICAL
    if any(t in (ner_dlp.DLP_EMAIL, ner_dlp.DLP_PHONE) for t in types):
        return SEV_HIGH
    return "medium"


# =====================================================================
# 正式分析入口
# =====================================================================

def analyze(
    db: Session | None,
    *,
    text: str,
    source: str = "critic",
    case_id: str | None = None,
    role: str = "agent",
    scenario: str = "refund",
    persist: bool = True,
) -> CriticVerdict:
    """Critic 正式分析：规则轨 + 语义轨 + NER DLP 脱敏 → 风险分与可追溯原因。

    - persist=False 时纯内存（db=None 也可用）——red_blue 脚本/单元测试用；
    - persist=True 时：verdict != PASS → 落 security_decisions + security_events；
      DLP 命中（PII）→ 无论判定，另落一条 DLP 事件（只记录，不改判定）。
    """
    text = text or ""
    rule = _sg_diagnose(text)                       # 规则轨（security_gateway）
    sem = _semantic_score(text)                     # 语义轨
    verdict, level, reasons = _combine(
        rule.injection_score, rule.rule_hits, sem)

    masked, entities = ner_dlp.mask(text, role=role, scenario=scenario)
    findings = [
        {
            "finding_type": e.entity_type,
            "position": e.start,
            "plain_snippet": e.masked[:50],
            "masked": e.masked,
            "action": e.action,
            "entities": e.as_dict(),
        }
        for e in entities
    ]

    result = CriticVerdict(
        verdict=verdict,
        risk_score=round(max(rule.injection_score, sem["score"]), 4),
        risk_level=level,
        reasons=reasons,
        rule_hits=rule.rule_hits,
        rule_score=rule.injection_score,
        semantic=sem,
        masked_input=masked,
        findings=findings,
    )

    if persist and db is not None:
        _persist(db, result, text=text, source=source, case_id=case_id)
    return result


def _sg_diagnose(text: str):
    from app.services import security_gateway as sg
    return sg.diagnose(text)


def _persist(
    db: Session, res: CriticVerdict, *,
    text: str, source: str, case_id: str | None,
) -> None:
    """拦截即录：SecurityDecision + SecurityEvent(s)。只记录证据，不改判定。"""
    from app.domain.models import SecurityDecision, new_uuid
    from app.infrastructure.audit import write_event

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if res.verdict != GATE_PASS:
        res.decision_id = new_uuid()
        db.add(SecurityDecision(
            id=res.decision_id,
            case_id=case_id,  # 可空（迁移 f3e4d5c6b7a9）：无案件归属时留 NULL 审计行
            source=source,
            gate_result=res.verdict,
            injection_score=res.risk_score,
            risk_level=res.risk_level,
            masked_input=res.masked_input,
            tool_name=None,
            rule_hits={
                **(dict(res.rule_hits)),
                "semantic_proto": res.semantic.get("proto"),
                "semantic_score": res.semantic.get("score"),
                "reasons": res.reasons[:20],
            },
        ))
        write_event(db, actor_id=None, case_id=case_id,
                    action="CRITIC_INTERCEPTED",
                    payload={"verdict": res.verdict, "score": res.risk_score,
                             "reason": (res.reasons or ["PASS"])[0][:120]})
        # INJECTION 事件（拦截类）
        evt = record_event(db, event_type=SEC_EV_INJECTION,
                           case_id=case_id, severity=res.risk_level.lower(),
                           detail=res.masked_input[:2000], audit_sha256=sha)
        db.flush()
        res.event_ids.append(evt.id)

    # DLP 事件（PII 检测；detail 只存掩码片段）
    pii_types = [f["finding_type"] for f in res.findings
                 if f["finding_type"] in ner_dlp.ALL_ENTITY_TYPES]
    if pii_types:
        sev = _dlp_severity(pii_types)
        evt2 = record_event(db, event_type=SEC_EV_DLP, case_id=case_id,
                            severity=sev,
                            detail=res.masked_input[:2000], audit_sha256=sha)
        db.flush()
        res.event_ids.append(evt2.id)

    db.commit()