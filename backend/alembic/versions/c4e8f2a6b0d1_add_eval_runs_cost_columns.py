"""add eval_runs cost columns (obs/9)

Revision ID: c4e8f2a6b0d1
Revises: 9d8e7f6a5b4c
Create Date: 2026-09-26 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8f2a6b0d1"
down_revision: str | Sequence[str] | None = "9d8e7f6a5b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """eval_runs 加 avg_cost / total_cost（人民币元；单价未配置时为 0）。"""
    op.add_column(
        "eval_runs",
        sa.Column("avg_cost", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "eval_runs",
        sa.Column("total_cost", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("eval_runs", "total_cost")
    op.drop_column("eval_runs", "avg_cost")
