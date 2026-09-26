"""add started_at / error_code for stuck-state reconciliation

Revision ID: 9d8e7f6a5b4c
Revises: 7a072178fba5
Create Date: 2026-09-25 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d8e7f6a5b4c"
down_revision: str | Sequence[str] | None = "7a072178fba5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """slides.started_at/error_code + project_outlines.started_at（#33 / ADR-0001）。

    started_at 允许为 NULL：历史上已卡住的行不可判定，不自动复位（见 ADR），
    只有新进入 generating 的行才会写入时间戳。
    """
    op.add_column("slides", sa.Column("error_code", sa.String(length=32), nullable=True))
    op.add_column("slides", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "project_outlines", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("project_outlines", "started_at")
    op.drop_column("slides", "started_at")
    op.drop_column("slides", "error_code")
