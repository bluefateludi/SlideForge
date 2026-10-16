"""allow unknown token and cost metrics for edit evaluation

Revision ID: e1f2a3b4c5d6
Revises: c4e8f2a6b0d1
"""

import sqlalchemy as sa

from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "c4e8f2a6b0d1"
branch_labels = None
depends_on = None

COLUMNS = ("avg_prompt_tokens", "avg_completion_tokens", "avg_cost", "total_cost")


def upgrade():
    for column in COLUMNS:
        op.alter_column("eval_runs", column, existing_type=sa.Float(), nullable=True)


def downgrade():
    for column in COLUMNS:
        # 老格式无法表达未知值；仅在明确降级时转换为兼容占位值。
        op.execute(sa.text(f"UPDATE eval_runs SET {column}=0 WHERE {column} IS NULL"))
        op.alter_column("eval_runs", column, existing_type=sa.Float(), nullable=False)
