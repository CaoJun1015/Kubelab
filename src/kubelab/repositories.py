"""Repository and Unit of Work boundaries for KubeLab local state."""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kubelab.db_models import (
    CheckResultRecord,
    GuidedLearningStateRecord,
    HintUsageRecord,
    LabSessionRecord,
    RetrospectiveRecord,
    SessionEventRecord,
    SessionEvidenceSnapshotRecord,
    VerificationRunRecord,
    utc_now,
)
from kubelab.redaction import redact_json
from kubelab.session_state import (
    ACTIVE_SESSION_STATUSES,
    GuidedLearningStateSnapshot,
    HintUsageSnapshot,
    LabSessionSnapshot,
    NewLabSession,
    RetrospectiveInput,
    RetrospectiveSnapshot,
    SessionEventSnapshot,
    SessionEvidenceSnapshot,
    SessionStateMachine,
    SessionStatus,
    ValidationStatus,
    VerificationCheckSnapshot,
    VerificationDetailSnapshot,
    VerificationPurpose,
    VerificationRunInput,
    VerificationRunSnapshot,
)


class SessionNotFoundError(LookupError):
    code = "SESSION_NOT_FOUND"


class ActiveSessionConflict(RuntimeError):
    code = "ACTIVE_SESSION_CONFLICT"

    def __init__(self, active: LabSessionSnapshot | None) -> None:
        self.active = active
        detail = f" Existing session: {active.id} ({active.status.value})." if active else ""
        super().__init__("Only one active lab session is allowed." + detail)


class SessionRepository:
    """Persistence operations for sessions and their lifecycle events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, new_session: NewLabSession) -> LabSessionSnapshot:
        record = LabSessionRecord(
            id=new_session.id,
            lab_id=new_session.lab_id,
            variant_id=new_session.variant_id,
            namespace=new_session.namespace,
            status=SessionStatus.PROVISIONING.value,
            context_name=new_session.context_name,
            context_fingerprint=new_session.context_fingerprint,
            created_at=new_session.created_at,
            started_at=new_session.started_at,
            completed_at=None,
            reset_count=0,
            last_error_code=None,
            last_error_context=None,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as exc:
            raise ActiveSessionConflict(self.get_active()) from exc
        self._add_event(
            record.id,
            event_type="session_created",
            from_status=None,
            to_status=SessionStatus.PROVISIONING,
            context=None,
            created_at=new_session.created_at,
        )
        self._session.flush()
        return _session_snapshot(record)

    def get(self, session_id: str) -> LabSessionSnapshot | None:
        record = self._session.get(LabSessionRecord, session_id)
        return _session_snapshot(record) if record else None

    def require(self, session_id: str) -> LabSessionSnapshot:
        snapshot = self.get(session_id)
        if snapshot is None:
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        return snapshot

    def get_active(self) -> LabSessionSnapshot | None:
        statement: Select[tuple[LabSessionRecord]] = select(LabSessionRecord).where(
            LabSessionRecord.status.in_(status.value for status in ACTIVE_SESSION_STATUSES)
        )
        record = self._session.scalar(statement)
        return _session_snapshot(record) if record else None

    def get_latest(self) -> LabSessionSnapshot | None:
        statement: Select[tuple[LabSessionRecord]] = select(LabSessionRecord).order_by(
            LabSessionRecord.created_at.desc(), LabSessionRecord.id.desc()
        )
        record = self._session.scalar(statement)
        return _session_snapshot(record) if record else None

    def list_all(self) -> tuple[LabSessionSnapshot, ...]:
        statement: Select[tuple[LabSessionRecord]] = select(LabSessionRecord).order_by(
            LabSessionRecord.created_at, LabSessionRecord.id
        )
        return tuple(_session_snapshot(record) for record in self._session.scalars(statement))

    def list_for_lab(self, lab_id: str) -> tuple[LabSessionSnapshot, ...]:
        statement: Select[tuple[LabSessionRecord]] = (
            select(LabSessionRecord)
            .where(LabSessionRecord.lab_id == lab_id)
            .order_by(LabSessionRecord.created_at, LabSessionRecord.id)
        )
        return tuple(_session_snapshot(record) for record in self._session.scalars(statement))

    def passed_variant_ids(self, lab_id: str) -> frozenset[str]:
        statement = (
            select(LabSessionRecord.variant_id)
            .join(SessionEventRecord, SessionEventRecord.session_id == LabSessionRecord.id)
            .where(
                LabSessionRecord.lab_id == lab_id,
                SessionEventRecord.event_type == "success_contract_passed",
            )
            .distinct()
        )
        return frozenset(self._session.scalars(statement))

    def has_event(self, session_id: str, event_type: str) -> bool:
        statement = select(SessionEventRecord.id).where(
            SessionEventRecord.session_id == session_id,
            SessionEventRecord.event_type == event_type,
        )
        return self._session.scalar(statement) is not None

    def passed_lab_ids(self) -> frozenset[str]:
        statement = (
            select(LabSessionRecord.lab_id)
            .join(SessionEventRecord, SessionEventRecord.session_id == LabSessionRecord.id)
            .where(SessionEventRecord.event_type == "success_contract_passed")
            .distinct()
        )
        return frozenset(self._session.scalars(statement))

    def transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        event_type: str,
        context: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_context: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> LabSessionSnapshot:
        record = self._require_record(session_id)
        current = SessionStatus(record.status)
        SessionStateMachine.require_transition(current, target)
        requested_timestamp = occurred_at or utc_now()
        created_at = _aware_required(record.created_at)
        timestamp = max(_aware_required(requested_timestamp), created_at)
        safe_context = _safe_dict(context)
        record.status = target.value
        if target is SessionStatus.READY and record.started_at is None:
            record.started_at = timestamp
        if target is SessionStatus.COMPLETED:
            record.completed_at = timestamp
        if target is SessionStatus.ERROR:
            record.last_error_code = error_code
            record.last_error_context = _safe_dict(error_context)
        elif target in {SessionStatus.READY, SessionStatus.COMPLETED}:
            record.last_error_code = None
            record.last_error_context = None
        self._add_event(
            session_id,
            event_type=event_type,
            from_status=current,
            to_status=target,
            context=safe_context,
            created_at=timestamp,
        )
        self._session.flush()
        return _session_snapshot(record)

    def increment_reset_count(self, session_id: str) -> int:
        record = self._require_record(session_id)
        record.reset_count += 1
        self._session.flush()
        return record.reset_count

    def list_events(self, session_id: str) -> tuple[SessionEventSnapshot, ...]:
        statement: Select[tuple[SessionEventRecord]] = (
            select(SessionEventRecord)
            .where(SessionEventRecord.session_id == session_id)
            .order_by(SessionEventRecord.created_at, SessionEventRecord.id)
        )
        return tuple(_event_snapshot(record) for record in self._session.scalars(statement))

    def record_event(
        self,
        session_id: str,
        event_type: str,
        *,
        context: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> SessionEventSnapshot:
        record = self._require_record(session_id)
        timestamp = occurred_at or utc_now()
        event = SessionEventRecord(
            session_id=session_id,
            event_type=event_type,
            from_status=record.status,
            to_status=record.status,
            context=_safe_dict(context),
            created_at=timestamp,
        )
        self._session.add(event)
        self._session.flush()
        return _event_snapshot(event)

    def _require_record(self, session_id: str) -> LabSessionRecord:
        record = self._session.get(LabSessionRecord, session_id)
        if record is None:
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        return record

    def _add_event(
        self,
        session_id: str,
        *,
        event_type: str,
        from_status: SessionStatus | None,
        to_status: SessionStatus | None,
        context: dict[str, Any] | None,
        created_at: datetime,
    ) -> None:
        self._session.add(
            SessionEventRecord(
                session_id=session_id,
                event_type=event_type,
                from_status=from_status.value if from_status else None,
                to_status=to_status.value if to_status else None,
                context=context,
                created_at=created_at,
            )
        )


class VerificationRepository:
    """Persist a validation run and all check results atomically."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: VerificationRunInput) -> None:
        self._session.add(
            VerificationRunRecord(
                id=run.id,
                session_id=run.session_id,
                purpose=run.purpose.value,
                status=run.status.value,
                reset_sequence=run.reset_sequence,
                checked_at=run.checked_at,
                duration_ms=run.duration_ms,
            )
        )
        self._session.flush()

        for result in run.results:
            self._session.add(
                CheckResultRecord(
                    run_id=run.id,
                    check_id=result.check_id,
                    check_type=result.check_type,
                    status=result.status.value,
                    expected=_safe_dict(result.expected) or {},
                    actual=_safe_dict(result.actual) or {},
                    message=str(redact_json(result.message)),
                    retryable=result.retryable,
                    duration_ms=result.duration_ms,
                )
            )
        self._session.flush()

    def list_for_session(self, session_id: str) -> tuple[VerificationRunSnapshot, ...]:
        statement = (
            select(VerificationRunRecord)
            .where(VerificationRunRecord.session_id == session_id)
            .order_by(VerificationRunRecord.checked_at, VerificationRunRecord.id)
        )
        return tuple(
            VerificationRunSnapshot(
                id=record.id,
                session_id=record.session_id,
                purpose=VerificationPurpose(record.purpose),
                status=ValidationStatus(record.status),
                checked_at=_aware_required(record.checked_at),
                duration_ms=record.duration_ms,
            )
            for record in self._session.scalars(statement)
        )

    def latest_for_session(self, session_id: str) -> VerificationDetailSnapshot | None:
        statement = (
            select(VerificationRunRecord)
            .where(VerificationRunRecord.session_id == session_id)
            .order_by(VerificationRunRecord.checked_at.desc(), VerificationRunRecord.id.desc())
        )
        record = self._session.scalar(statement)
        if record is None:
            return None
        result_statement = (
            select(CheckResultRecord)
            .where(CheckResultRecord.run_id == record.id)
            .order_by(CheckResultRecord.id)
        )
        results = tuple(
            VerificationCheckSnapshot(
                check_id=item.check_id,
                check_type=item.check_type,
                status=ValidationStatus(item.status),
                message=item.message,
                retryable=item.retryable,
                duration_ms=item.duration_ms,
            )
            for item in self._session.scalars(result_statement)
        )
        return VerificationDetailSnapshot(
            id=record.id,
            session_id=record.session_id,
            purpose=VerificationPurpose(record.purpose),
            status=ValidationStatus(record.status),
            checked_at=_aware_required(record.checked_at),
            duration_ms=record.duration_ms,
            results=results,
        )


class HintRepository:
    """Record each hint level once per session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_once(self, session_id: str, level: int, *, used_at: datetime | None = None) -> bool:
        record = HintUsageRecord(
            session_id=session_id,
            level=level,
            used_at=used_at or utc_now(),
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            return False
        return True

    def used_levels(self, session_id: str) -> tuple[int, ...]:
        statement: Select[tuple[int]] = (
            select(HintUsageRecord.level)
            .where(HintUsageRecord.session_id == session_id)
            .order_by(HintUsageRecord.level)
        )
        return tuple(self._session.scalars(statement))

    def record_request(
        self, session_id: str, level: int, *, used_at: datetime | None = None
    ) -> tuple[bool, int, int]:
        statement = select(HintUsageRecord).where(
            HintUsageRecord.session_id == session_id,
            HintUsageRecord.level == level,
        )
        record = self._session.scalar(statement)
        newly_unlocked = record is None
        if record is None:
            record = HintUsageRecord(
                session_id=session_id,
                level=level,
                used_at=used_at or utc_now(),
                request_count=1,
            )
            self._session.add(record)
        else:
            record.request_count += 1
        self._session.flush()
        return newly_unlocked, self.request_count(session_id), len(self.used_levels(session_id))

    def request_count(self, session_id: str) -> int:
        statement = select(HintUsageRecord).where(HintUsageRecord.session_id == session_id)
        return sum(record.request_count for record in self._session.scalars(statement))

    def list_for_session(self, session_id: str) -> tuple[HintUsageSnapshot, ...]:
        statement = (
            select(HintUsageRecord)
            .where(HintUsageRecord.session_id == session_id)
            .order_by(HintUsageRecord.used_at, HintUsageRecord.id)
        )
        return tuple(
            HintUsageSnapshot(
                level=record.level,
                used_at=_aware_required(record.used_at),
                request_count=record.request_count,
            )
            for record in self._session.scalars(statement)
        )


class GuidedLearningRepository:
    """Persist sanitized onboarding state and bounded Session evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_state(self) -> GuidedLearningStateSnapshot:
        record = self._session.get(GuidedLearningStateRecord, 1)
        if record is None:
            record = GuidedLearningStateRecord(id=1)
            self._session.add(record)
            self._session.flush()
        return _guided_learning_snapshot(record)

    def save_environment_report(
        self,
        *,
        status: str,
        report: dict[str, Any],
        checked_at: datetime | None = None,
    ) -> GuidedLearningStateSnapshot:
        record = self._session.get(GuidedLearningStateRecord, 1)
        if record is None:
            record = GuidedLearningStateRecord(id=1)
            self._session.add(record)
        timestamp = checked_at or utc_now()
        record.last_checked_at = timestamp
        record.last_environment_status = status
        record.last_environment_report = _safe_dict(report)
        if status in {"ready", "degraded"} and record.onboarding_completed_at is None:
            record.onboarding_completed_at = timestamp
        self._session.flush()
        return _guided_learning_snapshot(record)

    def add_evidence(
        self,
        session_id: str,
        *,
        trigger: str,
        capture_status: str,
        summary: dict[str, Any],
        captured_at: datetime | None = None,
    ) -> SessionEvidenceSnapshot:
        record = SessionEvidenceSnapshotRecord(
            session_id=session_id,
            trigger=trigger,
            capture_status=capture_status,
            summary=_safe_dict(summary) or {},
            captured_at=captured_at or utc_now(),
        )
        self._session.add(record)
        self._session.flush()
        return _evidence_snapshot(record)

    def list_evidence(self, session_id: str) -> tuple[SessionEvidenceSnapshot, ...]:
        statement = (
            select(SessionEvidenceSnapshotRecord)
            .where(SessionEvidenceSnapshotRecord.session_id == session_id)
            .order_by(SessionEvidenceSnapshotRecord.captured_at, SessionEvidenceSnapshotRecord.id)
        )
        return tuple(_evidence_snapshot(record) for record in self._session.scalars(statement))


class RetrospectiveRepository:
    """Upsert and retrieve the single retrospective owned by a session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        session_id: str,
        value: RetrospectiveInput,
        *,
        updated_at: datetime | None = None,
    ) -> RetrospectiveSnapshot:
        record = self._session.get(RetrospectiveRecord, session_id)
        if record is None:
            record = RetrospectiveRecord(session_id=session_id)
            self._session.add(record)
        safe = value.model_dump()
        for field, content in safe.items():
            setattr(record, field, str(redact_json(content)))
        record.updated_at = updated_at or utc_now()
        self._session.flush()
        return _retrospective_snapshot(record)

    def get(self, session_id: str) -> RetrospectiveSnapshot | None:
        record = self._session.get(RetrospectiveRecord, session_id)
        return _retrospective_snapshot(record) if record else None


class SqlAlchemyUnitOfWork:
    """Explicit transaction boundary shared by future CLI and Web services."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.sessions: SessionRepository
        self.verifications: VerificationRepository
        self.hints: HintRepository
        self.guided_learning: GuidedLearningRepository
        self.retrospectives: RetrospectiveRepository

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.sessions = SessionRepository(self._session)
        self.verifications = VerificationRepository(self._session)
        self.hints = HintRepository(self._session)
        self.guided_learning = GuidedLearningRepository(self._session)
        self.retrospectives = RetrospectiveRepository(self._session)
        return self

    def commit(self) -> None:
        self._require_session().commit()

    def rollback(self) -> None:
        self._require_session().rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._require_session()
        if exc_type is not None:
            session.rollback()
        session.close()
        self._session = None

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active.")
        return self._session


def _safe_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    redacted = redact_json(value)
    if not isinstance(redacted, dict):
        raise TypeError("Redacted JSON object must remain a dictionary.")
    return redacted


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _session_snapshot(record: LabSessionRecord) -> LabSessionSnapshot:
    return LabSessionSnapshot(
        id=record.id,
        lab_id=record.lab_id,
        variant_id=record.variant_id,
        namespace=record.namespace,
        status=SessionStatus(record.status),
        context_name=record.context_name,
        context_fingerprint=record.context_fingerprint,
        created_at=_aware_required(record.created_at),
        started_at=_aware(record.started_at),
        completed_at=_aware(record.completed_at),
        reset_count=record.reset_count,
        last_error_code=record.last_error_code,
        last_error_context=record.last_error_context,
    )


def _event_snapshot(record: SessionEventRecord) -> SessionEventSnapshot:
    return SessionEventSnapshot(
        id=record.id,
        session_id=record.session_id,
        event_type=record.event_type,
        from_status=SessionStatus(record.from_status) if record.from_status else None,
        to_status=SessionStatus(record.to_status) if record.to_status else None,
        context=record.context,
        created_at=_aware_required(record.created_at),
    )


def _guided_learning_snapshot(record: GuidedLearningStateRecord) -> GuidedLearningStateSnapshot:
    return GuidedLearningStateSnapshot(
        onboarding_completed_at=_aware(record.onboarding_completed_at),
        last_checked_at=_aware(record.last_checked_at),
        last_environment_status=record.last_environment_status,
        last_environment_report=record.last_environment_report,
    )


def _evidence_snapshot(record: SessionEvidenceSnapshotRecord) -> SessionEvidenceSnapshot:
    return SessionEvidenceSnapshot(
        id=record.id,
        session_id=record.session_id,
        trigger=record.trigger,
        capture_status=record.capture_status,
        summary=record.summary,
        captured_at=_aware_required(record.captured_at),
    )


def _retrospective_snapshot(record: RetrospectiveRecord) -> RetrospectiveSnapshot:
    return RetrospectiveSnapshot(
        session_id=record.session_id,
        symptom=record.symptom,
        impact=record.impact,
        investigation=record.investigation,
        root_cause=record.root_cause,
        resolution=record.resolution,
        prevention=record.prevention,
        interview_summary=record.interview_summary,
        updated_at=_aware_required(record.updated_at),
    )


def _aware_required(value: datetime) -> datetime:
    aware = _aware(value)
    if aware is None:  # pragma: no cover - non-null database constraint
        raise ValueError("Required database timestamp is missing.")
    return aware


__all__ = [
    "ActiveSessionConflict",
    "GuidedLearningRepository",
    "HintRepository",
    "RetrospectiveRepository",
    "SessionNotFoundError",
    "SessionRepository",
    "SqlAlchemyUnitOfWork",
    "VerificationRepository",
]
