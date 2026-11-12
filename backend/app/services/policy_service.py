# -*- coding: utf-8 -*-
"""策略中心服务（3.3.4）：按渠道取生效策略档 + 读写 profile/binding。

- graph.py 决策节点用 get_policy(case_id/渠道) 取阈值，取代原先写死的 _get_config；
- 策略变更不破坏三态与 HITL（红线），只改「秒退/挂起」判定阈值；
- 变更写 domain_events（append-only 审计），见 audit.py write_event。
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.models import (
    CHANNEL_DEFAULT,
    PolicyBinding,
    PolicyProfile,
    RefundCase,
)
from app.infrastructure.audit import write_event

# 缺省阈值（与迁移 d0a1b2c3v2001 里 default-mvp 档一致）
_DEFAULT = dict(
    amount_threshold=Decimal("300"),
    ocr_confidence=0.6,      # MVC 存整数百分比 60，转换为浮点 0.6
    fraud_pass=20,
    fraud_suspend=60,
    blacklist_autoreject=True,
    sla_remind_hr=4,
    sla_escalate_hr=24,
)

# graph 决策节点兜底（无任何策略档时）：对外 key，值=浮点阈值
DEFAULT_MVP = dict(
    amount_threshold=float(_DEFAULT["amount_threshold"]),
    ocr_confidence=_DEFAULT["ocr_confidence"],
    fraud_pass=_DEFAULT["fraud_pass"],
    fraud_suspend=_DEFAULT["fraud_suspend"],
    blacklist_autoreject=_DEFAULT["blacklist_autoreject"],
    sla_remind_hr=_DEFAULT["sla_remind_hr"],
    sla_escalate_hr=_DEFAULT["sla_escalate_hr"],
)


def _profile_as_dict(p: PolicyProfile) -> dict:
    return dict(
        id=p.id,
        name=p.name,
        amount_threshold=float(p.amount_threshold),
        ocr_confidence=p.ocr_confidence / 100.0,      # 存整数百分数，对外浮点
        fraud_pass=p.fraud_pass,
        fraud_suspend=p.fraud_suspend,
        blacklist_autoreject=bool(p.blacklist_autoreject),
        sla_remind_hr=p.sla_remind_hr,
        sla_escalate_hr=p.sla_escalate_hr,
        is_default=bool(p.is_default),
    )


def resolve_policy(db: Session, channel: str | None = None) -> PolicyProfile | None:
    """按渠道取绑定档；未绑定渠道或渠道无档 -> 回退 is_default 档；都无 -> None。"""
    ch = channel or CHANNEL_DEFAULT
    bind = (
        db.query(PolicyBinding)
        .filter(PolicyBinding.channel == ch)
        .first()
    )
    if bind is not None:
        return db.get(PolicyProfile, bind.profile_id)
    # 回退默认档
    return (
        db.query(PolicyProfile)
        .filter(PolicyProfile.is_default.is_(True))
        .first()
    )


def policy_values(db: Session, channel: str | None = None) -> dict:
    """返回渠道生效策略的阈值 dict（用于 graph 决策 / API 展示）。无档则用 MVP 默认。"""
    p = resolve_policy(db, channel)
    if p is None:
        return dict(DEFAULT_MVP)
    return _profile_as_dict(p)


def resolve_policy_for_case(db: Session, case: RefundCase) -> dict:
    """按案件渠道获取生效策略阈值（graph 决策节点入口）。"""
    return policy_values(db, case.channel or CHANNEL_DEFAULT)


# ---- 策略管理 API 用（3.3.4 配置中心）----
def list_profiles(db: Session) -> list[dict]:
    return [_resolve_as_dict(p) for p in db.query(PolicyProfile).all()]


def get_bindings(db: Session) -> list[dict]:
    """返回 (channel -> profile_id) 映射，供 UI 展示。"""
    return [
        {"channel": b.channel, "profile_id": b.profile_id}
        for b in db.query(PolicyBinding).all()
    ]


def update_profile(db: Session, profile_id: str, *, actor_id: str,
                   fields: dict) -> PolicyProfile:
    """更新策略档字段；写审计（POLICY_*）。fields 为白名单外键校验可写字段。"""
    p = db.get(PolicyProfile, profile_id)
    if p is None:
        raise KeyError(f"policy not found: {profile_id}")
    writable = ("amount_threshold", "ocr_confidence", "fraud_pass", "fraud_suspend",
                "blacklist_autoreject", "sla_remind_hr", "sla_escalate_hr")
    hunk: dict = {}
    for k in writable:
        if k in fields:
            v = fields[k]
            if k == "ocr_confidence" and isinstance(v, (int, float)) and v <= 1.0:
                v = int(round(v * 100))   # 浮点 -> 整数百分比
            setattr(p, k, v)
            hunk[k] = v
    db.commit()
    write_event(db=db, actor_id=actor_id, action="POLICY_UPDATED",
                payload={"profile_id": profile_id, "changes": hunk})
    db.commit()
    db.refresh(p)
    return p


def bind_channel(db: Session, profile_id: str, channel: str, *, actor_id: int) -> None:
    """把渠道绑定到策略档；同一渠道已有绑定则替换。写审计。"""
    existing = (
        db.query(PolicyBinding).filter(PolicyBinding.channel == channel).first()
    )
    if existing is not None:
        existing.profile_id = profile_id
    else:
        db.add(PolicyBinding(profile_id=profile_id, channel=channel))
    db.commit()
    write_event(db=db, actor_id=actor_id, action="POLICY_BOUND",
                payload={"channel": channel, "profile_id": profile_id})


def _resolve_as_dict(p: PolicyProfile) -> dict:
    # 管理 API 与图执行统一使用同一个序列化口径。旧实现引用了不存在的
    # ``DEFAULT``，导致 /policy/profiles 在有策略档时直接 500。
    return _profile_as_dict(p)
