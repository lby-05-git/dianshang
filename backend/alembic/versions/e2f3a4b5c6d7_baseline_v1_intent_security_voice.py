# -*- coding: utf-8 -*-
"""基线 V1.0（扩展能力最小融合）

1) refund_cases 补 `intent`（两级意图路由结果）与 `gateway_result`（输入安全网关结果）
2) 新增 5 张表：
   - intent_results  ：两级意图路由留存（只存原文 SHA-256，不落敏感明文）
   - security_decisions：输入安全网关/退款工具门禁判定（只存脱敏文本 masked_input）
   - dlp_findings    ：DLP 正则脱敏命中明细（命中片段落库前掩码）
   - voice_sessions  ：单会话语音闭环状态机（WS + Mock VAD/STT/TTS，插话即停）
   - coaching_sessions：客服陪练 1 场景（5 维评分 + 培训建议，禁副作用）

纯 add-only：不删旧表/不改旧列/不改旧语义；EVAL 隔离、既有索引/约束保持。
Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-08-31
"""
import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def _create_intent_results() -> None:
    op.create_table(
        "intent_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=False),
        sa.Column("level_1_rule", sa.String(32), nullable=True),
        sa.Column("level_2_llm", sa.String(32), nullable=True),
        sa.Column("final_intent", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("raw_sha256", sa.String(64), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False, server_default="LOW"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_intent_case", "intent_results", ["case_id"])


def _create_security_decisions() -> None:
    op.create_table(
        "security_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("gate_result", sa.String(16), nullable=False),
        sa.Column("injection_score", sa.Float(), nullable=True),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("masked_input", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("rule_hits", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_security_case", "security_decisions", ["case_id"])


def _create_dlp_findings() -> None:
    op.create_table(
        "dlp_findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=False),
        sa.Column("finding_type", sa.String(24), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("plain_snippet", sa.String(64), nullable=False),
        sa.Column("masked", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("idx_dlp_case", "dlp_findings", ["case_id"])


def _create_voice_sessions() -> None:
    op.create_table(
        "voice_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("refund_cases.id"), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("stt_text", sa.Text(), nullable=True),
        sa.Column("tts_cleared", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_case_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("session_id", name="uq_voice_session_id"),
    )


def _create_coaching_sessions() -> None:
    op.create_table(
        "coaching_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("turns", sa.JSON(), nullable=True),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def upgrade() -> None:
    op.add_column(
        "refund_cases",
        sa.Column("intent", sa.String(32), nullable=True),
    )
    op.add_column(
        "refund_cases",
        sa.Column("gateway_result", sa.String(16), nullable=False, server_default="PASS"),
    )
    _create_intent_results()
    _create_security_decisions()
    _create_dlp_findings()
    _create_voice_sessions()
    _create_coaching_sessions()


def downgrade() -> None:
    op.drop_table("coaching_sessions")
    op.drop_table("voice_sessions")
    op.drop_table("dlp_findings")
    op.drop_table("security_decisions")
    op.drop_table("intent_results")
    op.drop_column("refund_cases", "gateway_result")
    op.drop_column("refund_cases", "intent")