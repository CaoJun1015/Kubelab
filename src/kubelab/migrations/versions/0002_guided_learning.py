"""Add M5 guided learning persistence.

Revision ID: 0002_guided_learning
Revises: 0001_initial_persistence
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_guided_learning"
down_revision: str | None = "0001_initial_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hint_usage",
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "guided_learning_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_environment_status", sa.String(length=16)),
        sa.Column("last_environment_report", sa.JSON()),
        sa.CheckConstraint("id = 1", name="ck_guided_learning_state_singleton"),
        sa.CheckConstraint(
            "last_environment_status IS NULL OR "
            "last_environment_status IN ('ready','degraded','blocked')",
            name="ck_guided_learning_environment_status",
        ),
    )
    op.execute(
        "INSERT INTO guided_learning_state "
        "(id, onboarding_completed_at, last_checked_at, last_environment_status, "
        "last_environment_report) "
        "SELECT 1, CASE WHEN EXISTS (SELECT 1 FROM lab_session) "
        "THEN CURRENT_TIMESTAMP ELSE NULL END, NULL, NULL, NULL"
    )
    op.create_table(
        "session_evidence_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("capture_status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "capture_status IN ('captured','unavailable')",
            name="ck_session_evidence_capture_status",
        ),
    )
    op.create_index(
        "ix_session_evidence_session_captured",
        "session_evidence_snapshot",
        ["session_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_evidence_session_captured", table_name="session_evidence_snapshot"
    )
    op.drop_table("session_evidence_snapshot")
    op.drop_table("guided_learning_state")
    op.drop_column("hint_usage", "request_count")
