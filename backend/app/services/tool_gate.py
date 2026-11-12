# -*- coding: utf-8 -*-
"""工具门禁（tool gate）：执行前固定策略校验，覆盖 5 类高风险工具（V2.0 -3）。

- execute_refund      退款执行：状态可退(RUNNING) + 决策已批(APPROVED) + 核验(三项)全 MATCH
                       + 幂等(未退/未占流水号) —— 配合资金红线绝不自动退款（基线 V1.0 行为不变）；
- approve_refund      审批动作：仅 leader/admin 可批 + 未批准过(幂等) + 状态可批；
- document_generate   文档生成：内容不得含注入特征、输出文本不得含明文 PII；
- notification_send   通知发送：收件人非空 + 内容网关过滤（注入/PII 先脱敏）；
- external_query      外部查询：仅 http/https + SSRF 防线（禁内网/本机/链路本地地址）。

设计要点：
- 纯读校验 + 记录证据：任一不符 -> 拒绝并落 security_decisions(source=refund_tool)
  和一条 security_events(TOOL_GATE)（只记录，不改判定/退款成本）。
- 阈值白名单不可被 case 内容绕过；控制组（完全合规）必须 allow —— 护栏非砖墙。
- 红线：MISMATCH/UNKNOWN -> 强拒；幂等由调用方在 provider 之上用 SETNX 保证。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    REFUND_STATUS_FAILED,
    REFUND_STATUS_NONE,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    SecurityDecision,
    new_uuid,
)

TOOL_GATE_VERSION = "1.0"

# ---- 覆盖的工具白名单（：退款/审批/文档/通知/外部查询）----
TOOL_REFUND = "execute_refund"
TOOL_APPROVE = "approve_refund"
TOOL_DOCUMENT = "document_generate"
TOOL_NOTIFY = "notification_send"
TOOL_EXTERNAL_QUERY = "external_query"
COVERED_TOOLS = (TOOL_REFUND, TOOL_APPROVE, TOOL_DOCUMENT, TOOL_NOTIFY, TOOL_EXTERNAL_QUERY)

# 审批/通知等敏感动作的授予角色（与 app.api.deps 角色枚举一致）
APPROVER_ROLES = frozenset({"leader", "admin"})

# 可退状态集合（严格白名单，仅 RUNNING）
REFUNDABLE_STATUSES = frozenset({CASE_STATUS_RUNNING})
# 可退决策集合（严格白名单，仅 APPROVED）
REFUNDABLE_DECISIONS = frozenset({DECISION_APPROVED})
# 已退状态（幂等：存在即不可再退）
FINISHED_REFUND_STATUSES = frozenset({REFUND_STATUS_SUCCESS, REFUND_STATUS_PENDING})

# 核验字段（：三项核验必须全 MATCH）
VERIFY_FIELDS = ("order_verification", "product_verification", "price_verification")
MATCH = "MATCH"


@dataclass
class ToolGateDecision:
    """一次 tool gate 校验证据（可落库到 security_decisions + 事件）。"""
    allow: bool
    reason: list[str] = field(default_factory=list)
    gate_result: str = "PASS"           # PASS/BLOCK
    tool_name: str = TOOL_REFUND
    source: str = "refund_tool"
    case_id: str = ""

    def as_dict(self) -> dict:
        return {
            "allow": self.allow,
            "gate_result": self.gate_result,
            "tool_name": self.tool_name,
            "source": self.source,
            "reason": list(self.reason),
            "gate_version": TOOL_GATE_VERSION,
            "case_id": self.case_id,
        }


def _status_ok(case: RefundCase) -> tuple[bool, str]:
    if case.current_status in REFUNDABLE_STATUSES:
        return True, ""
    return False, f"状态 {case.current_status} 不可退（仅 RUNNING）"


def _decision_ok(case: RefundCase) -> tuple[bool, str]:
    if case.final_decision in REFUNDABLE_DECISIONS:
        return True, ""
    return False, f"决策 {case.final_decision} 未审批（仅 APPROVED）"


def _verify_ok(case: RefundCase) -> tuple[bool, str]:
    bad = [f for f in VERIFY_FIELDS if getattr(case, f, None) != MATCH]
    if not bad:
        return True, ""
    return False, f"核验未全 MATCH: {bad}"


def _idempotent_ok(case: RefundCase, *, refund_id_key: str | None = None,
                   has_refund_receipt: bool | None = None) -> tuple[bool, str]:
    """幂等校验：已退（SUCCESS/PENDING）或已占退款流水号 -> 拒绝，绝不重复退。

    refund_id_key / has_refund_receipt 由调用方注入（读 Redis refund:{case_id} /
    查 refund_transactions），纯 DB 态用 refund_status 判断。
    """
    if case.refund_status in FINISHED_REFUND_STATUSES:
        return False, f"已退（refund_status={case.refund_status}），幂等拒绝"
    if refund_id_key:
        return False, "已存在退款流水号，幂等拒绝"
    if has_refund_receipt:
        return False, "已存在退款回执，幂等拒绝"
    return True, ""


def check_refund_gate(
    db: Session,
    *,
    case: RefundCase,
    ref_reason: list[str] | None = None,
    refund_id_key: str | None = None,
    has_refund_receipt: bool | None = None,
    persist: bool = True,
) -> ToolGateDecision:
    """退款前置策略校验（纯读 + 可选落 security_decisions）。"""
    reason = list(ref_reason or [])
    for fn in (_status_ok, _decision_ok, _verify_ok):
        ok, msg = fn(case)
        if not ok:
            reason.append(msg)
    idem_ok, idem_msg = _idempotent_ok(case, refund_id_key=refund_id_key,
                                       has_refund_receipt=has_refund_receipt)
    if not idem_ok:
        reason.append(idem_msg)
    allow = not reason
    if persist and reason:
        _persist_block(db, case_id=case.id, reason=reason)
    return ToolGateDecision(allow=allow, reason=reason,
                            gate_result="PASS" if allow else "BLOCK",
                            tool_name=TOOL_REFUND, source="refund_tool",
                            case_id=case.id)


# =====================================================================
# 审批 / 文档 / 通知 / 外部查询 工具门禁（V2.0 覆盖面扩展）
# =====================================================================

def _decision(status: str, tool_name: str, case_id: str,
              allow: bool, reason: list[str]) -> ToolGateDecision:
    return ToolGateDecision(allow=allow, reason=reason,
                            gate_result="PASS" if allow else "BLOCK",
                            tool_name=tool_name, source="refund_tool",
                            case_id=case_id)


def check_approval_gate(
    db: Session, *,
    case: RefundCase, actor_role: str, persist: bool = True,
) -> ToolGateDecision:
    """审批门禁：仅 leader/admin 可批 + 幂等（未批准/未驳回）+ 状态可批（非已结）。"""
    reason: list[str] = []
    if actor_role not in APPROVER_ROLES:
        reason.append(f"角色 {actor_role} 无审批权（仅 leader/admin）")
    if case.final_decision == DECISION_APPROVED:
        reason.append(f"已审批通过（{DECISION_APPROVED}），幂等拒绝重复审批")
    if case.final_decision == DECISION_REJECTED:
        reason.append(f"已驳回（{DECISION_REJECTED}），须重新申诉流程")
    if case.current_status in (CASE_STATUS_COMPLETED,):
        reason.append(f"案件已 {CASE_STATUS_COMPLETED}，不可再审批")
    allow = not reason
    if persist and reason:
        _persist_block(db, case_id=case.id, reason=reason, tool_name=TOOL_APPROVE)
    return _decision("PASS" if allow else "BLOCK", TOOL_APPROVE, case.id, allow, reason)


def _content_reasons(content: str | None) -> list[str]:
    """对内容做注入网关 + DLP 校验，返回不满足项的理由。"""
    from app.services import security_gateway as sg
    reason: list[str] = []
    if content is None or not content.strip():
        reason.append("内容为空")
        return reason
    res = sg.diagnose(content)
    if res.gate_result in ("BLOCK", "REVIEW"):
        reason.append(f"内容含注入特征（risk={res.risk_level}）")
    if res.findings:
        types = sorted({f["finding_type"] for f in res.findings})
        reason.append(f"内容含明文敏感信息（{types}），输出前须先脱敏")
    return reason


def check_document_gate(
    db: Session, *, content: str | None, params: dict | None = None,
    persist: bool = True,
) -> ToolGateDecision:
    """文档生成门禁：内容不得含注入特征、不得含明文 PII（输出直拒 + 落证据）。"""
    reason = _content_reasons(content)
    # 业务参数白名单（文档名/模板等不携带注入内容）
    if params:
        names = str(params.get("document_name") or "")
        if len(names) > 0 and "\n" in names:
            reason.append("文档名含换行，拒绝（header 注入感染防线）")
    allow = not reason
    if persist and reason:
        _persist_block(db, case_id=params.get("case_id", ""), reason=reason,
                       tool_name=TOOL_DOCUMENT)
    return _decision("PASS" if allow else "BLOCK", TOOL_DOCUMENT,
                     params.get("case_id", ""), allow, reason)


def check_notification_gate(
    db: Session, *, content: str | None, recipients: list[str] | None = None,
    params: dict | None = None, persist: bool = True,
) -> ToolGateDecision:
    """通知门禁：收件人非空 + 内容网关过滤（注入/PII 必须先脱敏）。"""
    reason = list(_content_reasons(content))
    if not recipients:
        reason.append("收件人列表为空，无法发送")
    allow = not reason
    if persist and reason:
        _persist_block(db, case_id=(params or {}).get("case_id", ""), reason=reason,
                       tool_name=TOOL_NOTIFY)
    return _decision("PASS" if allow else "BLOCK", TOOL_NOTIFY,
                     (params or {}).get("case_id", ""), allow, reason)


def _is_private_fqdn(host: str) -> bool:
    """SSRF 防线：本机/内网/链路本地/保留 一律拒绝。"""
    import ipaddress
    host = (host or "").strip().lower().rstrip(".")
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                       # 域名不展开 DNS（沙箱内不解析外网）
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def check_external_query_gate(
    db: Session, *, url: str, params: dict | None = None, persist: bool = True,
) -> ToolGateDecision:
    """外部查询门禁：仅 http/https + 禁内网/本机/链路本地（SSRF 防线）。"""
    from urllib.parse import urlparse
    reason: list[str] = []
    try:
        p = urlparse(url or "")
    except ValueError:
        p = None
    if p is None or p.scheme not in ("http", "https"):
        reason.append(f"仅允许 http/https 外部查询（当前 scheme 非法）")
    elif not p.hostname:
        reason.append("URL 缺少主机名")
    elif _is_private_fqdn(p.hostname):
        reason.append(f"SSRF 防线：禁止访问内网/本机地址 {p.hostname}")
    allow = not reason
    if persist and reason:
        _persist_block(db, case_id=(params or {}).get("case_id", ""), reason=reason,
                       tool_name=TOOL_EXTERNAL_QUERY)
    return _decision("PASS" if allow else "BLOCK", TOOL_EXTERNAL_QUERY,
                     (params or {}).get("case_id", ""), allow, reason)


def check_tool_gate(
    db: Session, *,
    tool_name: str,
    actor_role: str = "agent",
    case: RefundCase | None = None,
    params: dict | None = None,
    persist: bool = True,
) -> ToolGateDecision:
    """按工具名分发的统一门禁入口（路由/worker 在调用 provider 前显式调用）。"""
    if tool_name == TOOL_REFUND:
        if case is None:
            return _decision("BLOCK", TOOL_REFUND, "", False, ["缺少 case 对象"])
        return check_refund_gate(db, case=case, persist=persist)
    if tool_name == TOOL_APPROVE:
        if case is None:
            return _decision("BLOCK", TOOL_APPROVE, "", False, ["缺少 case 对象"])
        return check_approval_gate(db, case=case, actor_role=actor_role, persist=persist)
    if tool_name == TOOL_DOCUMENT:
        return check_document_gate(db, content=(params or {}).get("content"),
                                   params=params, persist=persist)
    if tool_name == TOOL_NOTIFY:
        return check_notification_gate(db, content=(params or {}).get("content"),
                                       recipients=(params or {}).get("recipients"),
                                       params=params, persist=persist)
    if tool_name == TOOL_EXTERNAL_QUERY:
        return check_external_query_gate(db, url=(params or {}).get("url"),
                                         params=params, persist=persist)
    return _decision("BLOCK", tool_name, "", False, [f"未知工具 {tool_name}"])


def _persist_block(
    db: Session, *,
    case_id: str, reason: list[str],
    tool_name: str = TOOL_REFUND, source: str = "refund_tool",
) -> None:
    """落 security_decisions + security_events(TOOL_GATE) 审计证据。

    只记录；绝不改 case、不执行任何 provider 调用。
    """
    db.add(SecurityDecision(
        id=new_uuid(),
        case_id=case_id or new_uuid() if not case_id else case_id,
        source=source,
        gate_result="BLOCK",
        injection_score=0.0,              # tool gate 非注入分；rule_hits 存原因
        risk_level="MEDIUM",
        masked_input=reason[0] if reason else "tool gate block",
        tool_name=tool_name,
        rule_hits={"tool_gate_reasons": reason},
    ))
    db.flush()
    # 越权/工具拦截 即录 security_events（V2.0 -3）
    from app.services.security_events import SEC_EV_TOOL_GATE, record_event
    evt = record_event(db, event_type=SEC_EV_TOOL_GATE, case_id=case_id or None,
                       severity="high", detail=reason[0][:2000] if reason else "")
    db.flush()
    evt.disposition = "OPEN"
    db.commit()