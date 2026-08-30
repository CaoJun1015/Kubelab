"""Persist the deterministic scenario selected for each Session.

Revision ID: 0003_lab_variants
Revises: 0002_guided_learning
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_lab_variants"
down_revision: str | None = "0002_guided_learning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lab_session",
        sa.Column("variant_id", sa.String(length=63), nullable=False, server_default="baseline"),
    )
    op.create_index(
        "ix_lab_session_lab_variant_created",
        "lab_session",
        ["lab_id", "variant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_lab_session_lab_variant_created", table_name="lab_session")
    op.drop_column("lab_session", "variant_id")
