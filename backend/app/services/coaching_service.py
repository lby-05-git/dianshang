# -*- coding: utf-8 -*-
"""客服陪练（基线 V1.0）。

1 个客诉退赔场景（VOICE_SCENE_REFUND）：
- 回合收集：客服对场景的逐轮话术（turns，落库前脱敏，不落敏感原文）；
- 5 维评分（专业/情绪/合规/结案/效率）：确定性规则评分（不引 LLM）；
- 输出仅培训建议文字（suggestion）——**无任何资金/退款动作**（禁副作用红线）。

红线（裁决 D6）：只读评分建议；绝不调用退款/审批/状态变更。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import settings

# ---- 场景常量（V2.0 -5 多类陪练场景）----
SCENE_REFUND = "VOICE_SCENE_REFUND"
SCENE_APOLOGY = "VOICE_SCENE_APOLOGY"         # 致歉安抚
SCENE_ESCALATION = "VOICE_SCENE_ESCALATION"   # 投诉升级安抚
SCENE_GENERAL = "VOICE_SCENE_GENERAL"         # 通用咨询

# 5 维评分轴（键名与 coaching_sessions.score_json 对齐）
DIMENSIONS = ("专业", "情绪", "合规", "结案", "效率")

# 难度/情绪枚举（与 coaching_sessions.difficulty/emotion 列对齐）
DIFFICULTIES = ("easy", "medium", "hard")
EMOTIONS = ("calm", "upset", "escalated")

# 多场景目录（scene_id -> 元信息；start/score 据此校验与回显）
SCENARIOS: dict[str, dict] = {
    SCENE_REFUND: {
        "name": "退赔处理",
        "description": "客户申请退款/退换的赔付会话（5 维评分）。",
        "difficulties": DIFFICULTIES,
        "emotions": EMOTIONS,
    },
    SCENE_APOLOGY: {
        "name": "致歉安抚",
        "description": "服务差错场景，重点评分情绪共情与结案安抚。",
        "difficulties": DIFFICULTIES,
        "emotions": EMOTIONS,
    },
    SCENE_ESCALATION: {
        "name": "投诉升级",
        "description": "客户情绪激烈/扬言投诉，评分合规克制 + 情绪承接。",
        "difficulties": DIFFICULTIES,
        "emotions": EMOTIONS,
    },
    SCENE_GENERAL: {
        "name": "通用咨询",
        "description": "普通业务咨询（物流/订单/政策），评分专业与效率。",
        "difficulties": DIFFICULTIES,
        "emotions": EMOTIONS,
    },
}


def list_scenarios() -> list[dict]:
    """陪练场景目录（GET /api/v1/coaching/scenarios）"""
    return [
        {"scene_id": sid, **info} for sid, info in SCENARIOS.items()
    ]


def validate_scene_params(scene_id: str, difficulty: str | None,
                          emotion: str | None) -> tuple[str, str]:
    """校验 scene/difficulty/emotion；非法即 ValueError（路由转 400）。"""
    if scene_id not in SCENARIOS:
        raise ValueError(f"未知陪练场景: {scene_id}")
    diff = difficulty or "medium"
    mot = emotion or "calm"
    if diff not in DIFFICULTIES:
        raise ValueError(f"非法难度: {difficulty}")
    if mot not in EMOTIONS:
        raise ValueError(f"非法情绪: {emotion}")
    return diff, mot


@dataclass
class CoachingTurn:
    """单回合：客服话术（已脱敏）+ 客户问题上下文（已脱敏）。"""
    reply: str
    prompt: str = ""
    timestamp: float = 0.0

    def as_dict(self) -> dict:
        return {"reply": self.reply, "prompt": self.prompt}


@dataclass
class CoachingScoreResult:
    score_json: dict  # 5 维 {维度: 0-100}
    suggestion: str   # 培训建议（只读）
    total: float
    scene_id: str
    difficulty: str = "medium"
    emotion: str = "calm"
    explain: dict = field(default_factory=dict)   # 每维评分原因（标准评分维度解释）
    session_id: str = ""                           # 回放标识（路由落库行 id）

    def as_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "scene_name": SCENARIOS.get(self.scene_id, {}).get("name", ""),
            "difficulty": self.difficulty,
            "emotion": self.emotion,
            "score": self.score_json,
            "total": round(self.total, 1),
            "suggestion": self.suggestion,
            "explain": self.explain,
            "replay_session_id": self.session_id,
        }


# ---- 确定性评分规则 ----

def _mask_reply(text: str) -> str:
    """陪练回合脱敏：手机/身份证/银行卡/金额一律打码（不落敏感原文）。

    掩码格式对齐 security_gateway：手机 138****5678、身份证 110101********1234。
    """
    import re

    def _ph(m):
        s = m.group(0)
        return f"{s[:3]}****{s[-4:]}"

    text = re.sub(r"(?<!\d)(1[3-9]\d{9})(?!\d)", _ph, text or "")
    text = re.sub(r"(?<!\d)(\d{17}[0-9Xx])(?!\d)",
                  lambda m: m.group(0)[:6] + "********" + m.group(0)[-4:], text)
    text = re.sub(r"(?<!\d)(\d{13,19})(?!\d)",
                  lambda m: m.group(0)[:4] + " ******** " + m.group(0)[-4:], text)
    # 金额 xxx 元 -> 保留整数部分（杜绝侧录小数）
    text = re.sub(r"(\d{2,6})(\.\d{1,2})?\s*元", r"\1元", text)
    return text


def _score_dim(dim: str, turns: list[CoachingTurn]) -> tuple[float, str]:
    """确定性打分（0-100）+ 评分解释。规则启发式，供 5 维汇总（V2.0 标准评分维度）。

    返回 (score, reason)。reason 说明得分依据，供回放/解释/审计。
    """
    if not turns:
        return 0.0, "无回合数据"
    joined = " ".join(t.reply for t in turns)
    joined_cn = joined.lower()
    score = 60.0  # 基础分
    reasons: list[str] = ["基础分 60"]

    if dim == "专业":
        has_order = "订单" in joined_cn or "ord" in joined_cn
        has_amount = "元" in joined_cn or "金额" in joined_cn
        has_plan = "退款" in joined_cn or "处理" in joined_cn or "登记" in joined_cn
        if has_order and has_amount and has_plan:
            score += 25
            reasons.append("含订单号/金额/处理方案 3 要素 +25")
        elif has_order or has_amount:
            score += 10
            reasons.append("含订单号或金额 +10")
        else:
            reasons.append("缺订单/金额/方案要素")
    elif dim == "情绪":
        calm = any(k in joined_cn for k in ("请", "放心", "理解", "抱歉", "别担心"))
        if calm:
            score += 25
            reasons.append("含共情安抚词 +25")
        if any(k in joined_cn for k in ("不行", "没办法", "你自己", "不关我们")):
            score -= 30
            reasons.append("含生硬推诿表述 -30")
    elif dim == "合规":
        if any(k in joined_cn for k in ("跳过", "绕过", "伪造", "贷款", "骂", "你傻")):
            score = 20.0
            reasons.append("含跳过流程/伪造/攻击性表述，合规红线 20")
        elif "提供订单号" in joined_cn or "核验" in joined_cn:
            score += 20
            reasons.append("引导核验/提供订单号 +20")
    elif dim == "结案":
        if any(k in joined_cn for k in ("我将", "提交", "登记", "处理")):
            score += 20
            reasons.append("给出后续动作/时限 +20")
        else:
            reasons.append("未承诺后续动作/时限")
    elif dim == "效率":
        if 10 <= len(joined) <= 200:
            score += 20
            reasons.append("话术简洁适中 +20")
        elif len(joined) > 400:
            score -= 20
            reasons.append("话术冗长 >400 字 -20")
    return max(0.0, min(100.0, score)), "；".join(reasons)


# 难度/情绪调节（确定性，不做随机）：更难的场景/更激烈的情绪更严格
_DIFFICULTY_FACTOR = {"easy": 5.0, "medium": 0.0, "hard": -5.0}
_EMOTION_FACTOR = {"calm": 0.0, "upset": -2.0, "escalated": -5.0}


def score_coaching(
    scene_id: str, turns: list[CoachingTurn], *,
    difficulty: str = "medium", emotion: str = "calm",
    session_id: str = "",
) -> CoachingScoreResult:
    """5 维评分 + 培训建议（只读）。turns 已脱敏入库。

    V2.0：difficulty/emotion 影响分数（严格度），explain 给出每维评分原因，
    session_id 为回放标识（路由落库行）。禁副作用红线不变。
    """
    masked_turns = [CoachingTurn(reply=_mask_reply(t.reply), prompt=_mask_reply(t.prompt))
                    for t in turns]
    raw = {dim: _score_dim(dim, masked_turns) for dim in DIMENSIONS}
    score_json = {}
    explain = {}
    for dim in DIMENSIONS:
        base, reason = raw[dim]
        adj = base + _DIFFICULTY_FACTOR.get(difficulty, 0.0) \
                   + _EMOTION_FACTOR.get(emotion, 0.0)
        score_json[dim] = round(max(0.0, min(100.0, adj)), 1)
        if _DIFFICULTY_FACTOR.get(difficulty, 0.0):
            reason += f"；难度[{difficulty}]"
        if _EMOTION_FACTOR.get(emotion, 0.0):
            reason += f"；情绪[{emotion}]"
        explain[dim] = reason
    total = sum(score_json.values()) / len(score_json)

    # 培训建议：只给建议文字，无任何资金/退款动作（禁副作用红线）。
    suggestions = []
    if score_json["合规"] < 60:
        suggestions.append("合规话术不足：避免涉及跳过流程/伪造/承诺性表述。")
    if score_json["情绪"] < 60:
        suggestions.append("情绪安抚不足：多用“请放心、我理解您”等共情表达。")
    if score_json["专业"] < 60:
        suggestions.append("专业要素缺失：话术应包含订单号、金额与处理方案。")
    if score_json["结案"] < 60:
        suggestions.append("结案承诺缺失：明确后续动作与时限（如“我登记后将通知您”）。")
    if score_json["效率"] < 60:
        suggestions.append("效率偏弱：话术需简洁，避免冗长重复。")
    if not suggestions:
        suggestions.append("整体表现良好，保持现有服务标准。")

    return CoachingScoreResult(
        score_json=score_json,
        suggestion="\n".join(suggestions),
        total=total,
        scene_id=scene_id,
        difficulty=difficulty,
        emotion=emotion,
        explain=explain,
        session_id=session_id,
    )


def build_replay(row) -> dict:
    """回放数据（GET /api/v1/coaching/replay/{id}）：脱敏回合 + 评分 + 解释 + 建议。

    `row` 为 CoachingSession ORM 行。只读组装，无任何资金/退款动作。
    """
    return {
        "session_id": row.id,
        "scene_id": row.scene_id,
        "scene_name": SCENARIOS.get(row.scene_id, {}).get("name", ""),
        "difficulty": row.difficulty or "medium",
        "emotion": row.emotion or "calm",
        "turns": row.turns or [],
        "score": row.score_json or {},
        "suggestion": row.suggestion,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def mask_turn(text: str) -> str:
    """对外暴露的回合脱敏（供 routing 层入库前调用）。"""
    return _mask_reply(text)