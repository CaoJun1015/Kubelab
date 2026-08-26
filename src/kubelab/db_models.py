"""SQLAlchemy 2 mappings for KubeLab local persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kubelab.session_state import SessionStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LabSessionRecord(Base):
    __tablename__ = "lab_session"
    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning','ready','in_progress','passed','resetting',"
            "'cleaning','completed','error')",
            name="ck_lab_session_status",
        ),
        Index(
            "uq_lab_session_single_active",
            text("1"),
            unique=True,
            sqlite_where=text(
                "status IN ('provisioning','ready','in_progress','passed','resetting',"
                "'cleaning','error')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    lab_id: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SessionStatus.PROVISIONING.value
    )
    context_name: Mapped[str] = mapped_column(String(253), nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    last_error_context: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SessionEventRecord(Base):
    __tablename__ = "session_event"
    __table_args__ = (Index("ix_session_event_session_created", "session_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("lab_session.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VerificationRunRecord(Base):
    __tablename__ = "verification_run"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('initial','success_contract','manual')",
            name="ck_verification_run_purpose",
        ),
        CheckConstraint("status IN ('passed','failed','error')", name="ck_verification_run_status"),
        Index("ix_verification_run_session_checked", "session_id", "checked_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("lab_session.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reset_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class CheckResultRecord(Base):
    __tablename__ = "check_result"
    __table_args__ = (
        CheckConstraint("status IN ('passed','failed','error')", name="ck_check_result_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("verification_run.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[str] = mapped_column(String(64), nullable=False)
    check_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actual: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class HintUsageRecord(Base):
    __tablename__ = "hint_usage"
    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 3", name="ck_hint_usage_level"),
        Index("uq_hint_usage_session_level", "session_id", "level", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("lab_session.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RetrospectiveRecord(Base):
    __tablename__ = "retrospective"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("lab_session.id", ondelete="CASCADE"), primary_key=True
    )
    symptom: Mapped[str] = mapped_column(Text, nullable=False, default="")
    impact: Mapped[str] = mapped_column(Text, nullable=False, default="")
    investigation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    root_cause: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prevention: Mapped[str] = mapped_column(Text, nullable=False, default="")
    interview_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "Base",
    "CheckResultRecord",
    "HintUsageRecord",
    "LabSessionRecord",
    "RetrospectiveRecord",
    "SessionEventRecord",
    "VerificationRunRecord",
    "utc_now",
]
