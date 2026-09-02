"""Persist local package inventory and immutable Session provenance.

Revision ID: 0004_lab_packages
Revises: 0003_lab_variants
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_lab_packages"
down_revision: str | None = "0003_lab_variants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lab_package",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("lab_id", sa.String(length=64), nullable=False),
        sa.Column("package_version", sa.String(length=64), nullable=False),
        sa.Column("publisher_id", sa.String(length=63), nullable=False),
        sa.Column("publisher_name", sa.String(length=80), nullable=False),
        sa.Column("requires_kubelab", sa.String(length=120), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("archive_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_removal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged','enabled','disabled','pending_removal','removed')",
            name="ck_lab_package_status",
        ),
        sa.PrimaryKeyConstraint("sha256"),
    )
    op.create_index(
        "uq_lab_package_identity_version",
        "lab_package",
        ["lab_id", "publisher_id", "package_version"],
        unique=True,
    )
    op.create_index(
        "uq_lab_package_one_enabled",
        "lab_package",
        ["lab_id"],
        unique=True,
        sqlite_where=sa.text("status = 'enabled'"),
    )
    op.create_index(
        "ix_lab_package_lab_status",
        "lab_package",
        ["lab_id", "status"],
    )
    op.create_table(
        "lab_package_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["package_sha256"], ["lab_package.sha256"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lab_package_event_package_created",
        "lab_package_event",
        ["package_sha256", "created_at"],
    )
    # Direct ALTER preserves every child row and the expression-based single-active
    # Session index. Alembic's SQLite batch rebuild would cascade-delete Session
    # children while replacing lab_session.
    op.add_column(
        "lab_session",
        sa.Column(
            "lab_source",
            sa.String(length=16),
            nullable=False,
            server_default="builtin",
        ),
    )
    op.add_column("lab_session", sa.Column("package_sha256", sa.String(length=64), nullable=True))
    op.add_column("lab_session", sa.Column("lab_public_snapshot", sa.JSON(), nullable=True))
    op.add_column("lab_session", sa.Column("scenario_public_snapshot", sa.JSON(), nullable=True))
    op.execute(
        sa.text(
            "CREATE TRIGGER ck_lab_session_package_source_insert "
            "BEFORE INSERT ON lab_session FOR EACH ROW "
            "WHEN NEW.lab_source NOT IN ('builtin','local_package') "
            "OR (NEW.lab_source = 'builtin' AND NEW.package_sha256 IS NOT NULL) "
            "OR (NEW.lab_source = 'local_package' AND "
            "(NEW.package_sha256 IS NULL OR NOT EXISTS "
            "(SELECT 1 FROM lab_package WHERE sha256 = NEW.package_sha256))) "
            "BEGIN SELECT RAISE(ABORT, 'invalid lab package source'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER ck_lab_session_package_source_update "
            "BEFORE UPDATE OF lab_source, package_sha256 ON lab_session FOR EACH ROW "
            "WHEN NEW.lab_source NOT IN ('builtin','local_package') "
            "OR (NEW.lab_source = 'builtin' AND NEW.package_sha256 IS NOT NULL) "
            "OR (NEW.lab_source = 'local_package' AND "
            "(NEW.package_sha256 IS NULL OR NOT EXISTS "
            "(SELECT 1 FROM lab_package WHERE sha256 = NEW.package_sha256))) "
            "BEGIN SELECT RAISE(ABORT, 'invalid lab package source'); END"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER ck_lab_session_package_source_update"))
    op.execute(sa.text("DROP TRIGGER ck_lab_session_package_source_insert"))
    op.drop_column("lab_session", "scenario_public_snapshot")
    op.drop_column("lab_session", "lab_public_snapshot")
    op.drop_column("lab_session", "package_sha256")
    op.drop_column("lab_session", "lab_source")
    op.drop_index("ix_lab_package_event_package_created", table_name="lab_package_event")
    op.drop_table("lab_package_event")
    op.drop_index("ix_lab_package_lab_status", table_name="lab_package")
    op.drop_index("uq_lab_package_one_enabled", table_name="lab_package")
    op.drop_index("uq_lab_package_identity_version", table_name="lab_package")
    op.drop_table("lab_package")
