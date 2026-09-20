"""create eval_runs

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-09-20 23:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """评测 run 结果单表（eval#4）：聚合指标独立列 + 逐题明细/分类均分 JSONB。"""
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cases_version", sa.String(length=100), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("ok_cases", sa.Integer(), nullable=False),
        sa.Column("success_rate", sa.Float(), nullable=False),
        sa.Column("failure_rate", sa.Float(), nullable=False),
        sa.Column("schema_valid_cases", sa.Integer(), nullable=False),
        sa.Column("schema_scored_cases", sa.Integer(), nullable=False),
        sa.Column("schema_valid_rate", sa.Float(), nullable=False),
        sa.Column("pages_met_cases", sa.Integer(), nullable=False),
        sa.Column("pages_scored_cases", sa.Integer(), nullable=False),
        sa.Column("pages_met_rate", sa.Float(), nullable=False),
        sa.Column("export_succeeded_cases", sa.Integer(), nullable=False),
        sa.Column("avg_judge_coverage", sa.Float(), nullable=True),
        sa.Column("avg_judge_score", sa.Float(), nullable=True),
        sa.Column("judge_failed_cases", sa.Integer(), nullable=False),
        sa.Column("doc_total_numbers", sa.Integer(), nullable=False),
        sa.Column("doc_fabricated_numbers", sa.Integer(), nullable=False),
        sa.Column("hallucination_rate", sa.Float(), nullable=True),
        sa.Column("avg_elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("avg_prompt_tokens", sa.Float(), nullable=False),
        sa.Column("avg_completion_tokens", sa.Float(), nullable=False),
        sa.Column("total_failed_slides", sa.Integer(), nullable=False),
        sa.Column("total_slides", sa.Integer(), nullable=False),
        sa.Column("retry_slide_rate", sa.Float(), nullable=True),
        sa.Column("total_retried_slides", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("category_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_eval_runs_cases_version"), "eval_runs", ["cases_version"], unique=False
    )
    op.create_index(op.f("ix_eval_runs_created_at"), "eval_runs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_eval_runs_created_at"), table_name="eval_runs")
    op.drop_index(op.f("ix_eval_runs_cases_version"), table_name="eval_runs")
    op.drop_table("eval_runs")
