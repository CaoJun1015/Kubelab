"""Create the M1-05 local persistence schema.

Revision ID: 0001_initial_persistence
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_persistence"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_session",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("lab_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=63), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context_name", sa.String(length=253), nullable=False),
        sa.Column("context_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("reset_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("last_error_context", sa.JSON()),
        sa.CheckConstraint(
            "status IN ('provisioning','ready','in_progress','passed','resetting',"
            "'cleaning','completed','error')",
            name="ck_lab_session_status",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_lab_session_single_active ON lab_session ((1)) "
        "WHERE status IN "
        "('provisioning','ready','in_progress','passed','resetting','cleaning','error')"
    )

    op.create_table(
        "session_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("from_status", sa.String(length=32)),
        sa.Column("to_status", sa.String(length=32)),
        sa.Column("context", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_session_event_session_created",
        "session_event",
        ["session_id", "created_at"],
    )

    op.create_table(
        "verification_run",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reset_sequence", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('initial','success_contract','manual')",
            name="ck_verification_run_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('passed','failed','error')", name="ck_verification_run_status"
        ),
    )
    op.create_index(
        "ix_verification_run_session_checked",
        "verification_run",
        ["session_id", "checked_at"],
    )

    op.create_table(
        "check_result",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("verification_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("check_id", sa.String(length=64), nullable=False),
        sa.Column("check_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("actual", sa.JSON(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('passed','failed','error')", name="ck_check_result_status"),
    )

    op.create_table(
        "hint_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("level BETWEEN 1 AND 3", name="ck_hint_usage_level"),
    )
    op.create_index(
        "uq_hint_usage_session_level",
        "hint_usage",
        ["session_id", "level"],
        unique=True,
    )

    op.create_table(
        "retrospective",
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("lab_session.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("symptom", sa.Text(), nullable=False, server_default=""),
        sa.Column("impact", sa.Text(), nullable=False, server_default=""),
        sa.Column("investigation", sa.Text(), nullable=False, server_default=""),
        sa.Column("root_cause", sa.Text(), nullable=False, server_default=""),
        sa.Column("resolution", sa.Text(), nullable=False, server_default=""),
        sa.Column("prevention", sa.Text(), nullable=False, server_default=""),
        sa.Column("interview_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("retrospective")
    op.drop_index("uq_hint_usage_session_level", table_name="hint_usage")
    op.drop_table("hint_usage")
    op.drop_table("check_result")
    op.drop_index("ix_verification_run_session_checked", table_name="verification_run")
    op.drop_table("verification_run")
    op.drop_index("ix_session_event_session_created", table_name="session_event")
    op.drop_table("session_event")
    op.drop_index("uq_lab_session_single_active", table_name="lab_session")
    op.drop_table("lab_session")
