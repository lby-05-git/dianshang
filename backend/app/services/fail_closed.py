# -*- coding: utf-8 -*-
"""V3.0 V3-3 安全服务故障 Fail-Closed。

安全服务（输入网关 / Critic / DLP NER / 安全事件库）不可用时：
- **高风险操作**（退款执行 / 审批等资金面操作）-> 一律 Fail-Closed **DENY**，
  绝不因安全服务故障而放开退款；
- **普通查询** -> 按已批准降级策略处理：mask_only 时返回「仅脱敏结果」DEGRADE
  （绝不止漏明文/原件）；无批准策略（默认）-> **DENY**（零信任默认拒绝）；
- 服务恢复健康 -> 立即恢复 ALLOW（自愈）。

设计：
- 健康探测 `probe(service)`：默认全健康；测试用 `cause_health()` 注入故障
  （内存钩子，真实部署可替换为连接/心跳探测）；
- 降级策略读 system_configs `security.fail_closed.degrade_mode`（批准链由版本中心
  登记 security_policy 消费方评审）；mode 只允许 mask_only / deny，其余一律 deny；
- DENY/DEGRADE 落一条 DomainEvent（append-only 审计），只留痕、不改判定。

红线：Fail-Closed 绝不返回明文原件；DEGRADE 结果必须已脱敏（masked=True）。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.domain.models import SystemConfig
from app.infrastructure.audit import write_event

# ---- 安全服务名 ----
SVC_SECURITY_GATEWAY = "security_gateway"     # 输入网关（注入/DLP）
SVC_LLM_CRITIC = "llm_critic"                 # Critic 语义风险
SVC_NER_DLP = "ner_dlp"                       # DLP NER 脱敏
SVC_SECURITY_EVENTS = "security_events"       # 安全事件工作台
SECURITY_SERVICES = (SVC_SECURITY_GATEWAY, SVC_LLM_CRITIC,
                     SVC_NER_DLP, SVC_SECURITY_EVENTS)

# ---- 操作类型 ----
OP_HIGH_RISK = "high_risk"      # 退款执行/审批等资金面高风险操作
OP_QUERY = "query"              # 普通查询（脱敏/展示/导出等）

# ---- 处置 ----
ACT_ALLOW = "ALLOW"
ACT_DENY = "DENY"
ACT_DEGRADE = "DEGRADE"

# 查询降级模式：mask_only=只回脱敏结果；deny=直接拒绝（默认/未批准）
MODE_MASK_ONLY = "mask_only"
MODE_DENY = "deny"
_VALID_MODES = (MODE_MASK_ONLY, MODE_DENY)

DEGRADE_CONFIG_KEY = "security.fail_closed.degrade_mode"
DEGRADE_CONFIG_DESCRIPTION = (
    "安全服务故障时普通查询的降级策略：mask_only（只回脱敏结果）/ deny（默认，拒绝）。"
    "该 key 由版本中心 security_policy 实体评审后人工写入，未配置一律 deny。")

# ---- 健康探测（内存钩子；测试用 cause_health 注入故障）----
_HEALTH_OVERRIDES: dict[str, bool] = {}


def cause_health(service: str, healthy: bool) -> None:
    """测试/演练钩子：显式设置某安全服务健康状态（False=故障注入）。"""
    if service not in SECURITY_SERVICES:
        raise ValueError(f"未知安全服务 {service}（可选 {SECURITY_SERVICES}）")
    _HEALTH_OVERRIDES[service] = bool(healthy)


def reset_health() -> None:
    """恢复全部服务为健康（测试 teardown / 演练结束）。"""
    _HEALTH_OVERRIDES.clear()


def probe(service: str) -> bool:
    """当前健康状态（无覆盖默认健康）。"""
    return _HEALTH_OVERRIDES.get(service, True)


def health_report() -> dict:
    return {"services": {s: probe(s) for s in SECURITY_SERVICES}}


def _degrade_mode(db: Session | None) -> str:
    """读取已批准降级策略；未配置/非法一律 deny（零信任默认拒绝）。"""
    if db is None:
        return MODE_DENY
    row = db.get(SystemConfig, DEGRADE_CONFIG_KEY)
    if row is None:
        return MODE_DENY
    raw = (row.cfg_value or {}).get("mode", MODE_DENY)
    return raw if raw in _VALID_MODES else MODE_DENY


@dataclass
class FailClosedDecision:
    """一次 Fail-Closed 判定证据。"""

    service: str
    healthy: bool
    operation: str
    action: str          # ALLOW / DENY / DEGRADE
    reason: str
    mask_only: bool = False    # DEGRADE 时：返回结果必须已脱敏
    degrade_mode: str = MODE_DENY

    def as_dict(self) -> dict:
        return {
            "service": self.service,
            "healthy": self.healthy,
            "operation": self.operation,
            "action": self.action,
            "reason": self.reason,
            "mask_only": self.mask_only,
            "degrade_mode": self.degrade_mode,
        }


def evaluate_fail_closed(
    db: Session, *, operation: str, service: str, persist: bool = True,
) -> FailClosedDecision:
    """Fail-Closed 判定主入口。

    - operation=high_risk：服务不健康 -> DENY（资金面绝不因安全故障放开）；
    - operation=query：服务不健康 -> 按批准降级策略 DEGRADE(mask_only)/DENY；
    - 服务健康 -> ALLOW。
    persist=True 时 DENY/DEGRADE 落 DomainEvent 审计（append-only，只留痕）。
    """
    if service not in SECURITY_SERVICES:
        raise ValueError(f"未知安全服务 {service}（可选 {SECURITY_SERVICES}）")
    if operation not in (OP_HIGH_RISK, OP_QUERY):
        raise ValueError(f"未知操作类型 {operation}（可选 {OP_HIGH_RISK}/{OP_QUERY}）")
    healthy = probe(service)
    reason = ""
    action = ACT_ALLOW
    mask_only = False
    mode = MODE_DENY
    if not healthy:
        if operation == OP_HIGH_RISK:
            action = ACT_DENY
            reason = f"安全服务 {service} 故障，高风险操作 Fail-Closed（拒绝执行）"
        else:
            mode = _degrade_mode(db)
            if mode == MODE_MASK_ONLY:
                action = ACT_DEGRADE
                mask_only = True
                reason = (f"安全服务 {service} 故障，按已批准降级策略返回脱敏结果"
                          f"（决不返回明文原件）")
            else:
                action = ACT_DENY
                reason = (f"安全服务 {service} 故障，且无已批准降级策略"
                          f"（degrade_mode={mode}），查询默认拒绝")
    if persist and action in (ACT_DENY, ACT_DEGRADE):
        write_event(db, action="SECURITY_FAIL_CLOSED",
                    payload={"service": service, "operation": operation,
                             "action": action, "healthy": healthy,
                             "reason": reason})
        db.commit()
    return FailClosedDecision(
        service=service, healthy=healthy, operation=operation,
        action=action, reason=reason, mask_only=mask_only, degrade_mode=mode,
    )


def guard_high_risk(db: Session, *, service: str = SVC_SECURITY_GATEWAY,
                    persist: bool = True) -> FailClosedDecision:
    """资金面高风险操作 Fail-Closed 快捷入口（退款执行前调用）。"""
    return evaluate_fail_closed(db, operation=OP_HIGH_RISK, service=service,
                                persist=persist)


def guard_query(db: Session, *, service: str = SVC_NER_DLP,
                persist: bool = True) -> FailClosedDecision:
    """普通查询降级快捷入口（脱敏/导出前调用）。"""
    return evaluate_fail_closed(db, operation=OP_QUERY, service=service,
                                persist=persist)