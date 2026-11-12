# -*- coding: utf-8 -*-
"""评测中心 V2.0 表（Evaluation Suite/Case/Run/Result/Artifact + Prompt Version）。

迁移链：e1f2a3b4c5d6 (V1 eval 列) -> f1a2b3c4d5e6 (V2 评测中心)。
只新增表，不改既有表结构；downgrade 按 FK 依赖顺序（子→父）删除。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- eval_suites ----
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("modalities", sa.JSON(), nullable=True),
        sa.Column("required_capabilities", sa.JSON(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_suite_name", "eval_suites", ["name"], unique=False)

    # ---- eval_cases ----
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suite_id", sa.String(length=36), nullable=False),
        sa.Column("case_ref_id", sa.String(length=20), nullable=False),
        sa.Column("case_type", sa.String(length=50), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("constraints", sa.JSON(), nullable=True),
        sa.Column("extensions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_case_suite", "eval_cases", ["suite_id"], unique=False)
    op.create_index("idx_eval_case_ref", "eval_cases", ["suite_id", "case_ref_id"], unique=False)

    # ---- eval_runs ----
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("schema_version", sa.String(length=20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("suite_id", sa.String(length=36), nullable=True),
        sa.Column("suite_version", sa.String(length=20), nullable=True),
        sa.Column("dataset_checksum", sa.String(length=64), nullable=True),
        sa.Column("target_type", sa.String(length=30), nullable=True),
        sa.Column("base_url", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("environment", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("performance", sa.JSON(), nullable=True),
        sa.Column("cost", sa.JSON(), nullable=True),
        sa.Column("prompt_variant_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_baseline_id", sa.String(length=36), nullable=True),
        sa.Column("report_path", sa.String(length=300), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_run_status", "eval_runs", ["status"], unique=False)
    op.create_index("idx_eval_run_suite", "eval_runs", ["suite_id"], unique=False)
    op.create_index("idx_eval_run_created", "eval_runs", ["created_at"], unique=False)
    op.create_unique_constraint("uq_eval_runs_run_id", "eval_runs", ["run_id"])

    # ---- eval_case_results ----
    op.create_table(
        "eval_case_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("case_ref_id", sa.String(length=20), nullable=False),
        sa.Column("case_type", sa.String(length=50), nullable=True),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("dimensions", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("judge_version", sa.String(length=10), nullable=True),
        sa.Column("degraded", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("degraded_reason", sa.Text(), nullable=True),
        sa.Column("execution", sa.JSON(), nullable=True),
        sa.Column("candidate", sa.JSON(), nullable=True),
        sa.Column("llm_judge", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_case_result_run", "eval_case_results", ["run_id"], unique=False)
    op.create_index(
        "idx_eval_case_result_case",
        "eval_case_results",
        ["run_id", "case_ref_id"],
        unique=False,
    )

    # ---- eval_artifacts ----
    op.create_table(
        "eval_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("case_ref_id", sa.String(length=20), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("mime_type", sa.String(length=80), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("storage_ref", sa.String(length=300), nullable=True),
        sa.Column("sensitivity", sa.String(length=20), nullable=False),
        sa.Column("approved", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_eval_artifact_run", "eval_artifacts", ["run_id"], unique=False)
    op.create_index(
        "idx_eval_artifact_case",
        "eval_artifacts",
        ["run_id", "case_ref_id"],
        unique=False,
    )

    # ---- prompt_versions ----
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("agent_name", sa.String(length=50), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_checksum", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("creator_id", sa.String(length=36), nullable=True),
        sa.Column("approver_id", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("is_baseline", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_prompt_version_agent", "prompt_versions", ["agent_name"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_prompt_version_agent", table_name="prompt_versions")
    op.drop_table("prompt_versions")

    op.drop_index("idx_eval_artifact_case", table_name="eval_artifacts")
    op.drop_index("idx_eval_artifact_run", table_name="eval_artifacts")
    op.drop_table("eval_artifacts")

    op.drop_index("idx_eval_case_result_case", table_name="eval_case_results")
    op.drop_index("idx_eval_case_result_run", table_name="eval_case_results")
    op.drop_table("eval_case_results")

    op.drop_constraint("uq_eval_runs_run_id", "eval_runs", type_="unique")
    op.drop_index("idx_eval_run_created", table_name="eval_runs")
    op.drop_index("idx_eval_run_suite", table_name="eval_runs")
    op.drop_index("idx_eval_run_status", table_name="eval_runs")
    op.drop_table("eval_runs")

    op.drop_index("idx_eval_case_ref", table_name="eval_cases")
    op.drop_index("idx_eval_case_suite", table_name="eval_cases")
    op.drop_table("eval_cases")

    op.drop_index("idx_eval_suite_name", table_name="eval_suites")
    op.drop_table("eval_suites")