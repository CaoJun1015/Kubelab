"""Pure domain state machine and persistence DTOs for lab sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    RESETTING = "resetting"
    CLEANING = "cleaning"
    COMPLETED = "completed"
    ERROR = "error"


ACTIVE_SESSION_STATUSES = frozenset(
    {
        SessionStatus.PROVISIONING,
        SessionStatus.READY,
        SessionStatus.IN_PROGRESS,
        SessionStatus.PASSED,
        SessionStatus.RESETTING,
        SessionStatus.CLEANING,
        SessionStatus.ERROR,
    }
)

_ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.PROVISIONING: frozenset(
        {SessionStatus.READY, SessionStatus.ERROR, SessionStatus.CLEANING}
    ),
    SessionStatus.READY: frozenset(
        {
            SessionStatus.IN_PROGRESS,
            SessionStatus.RESETTING,
            SessionStatus.CLEANING,
            SessionStatus.ERROR,
        }
    ),
    SessionStatus.IN_PROGRESS: frozenset(
        {
            SessionStatus.PASSED,
            SessionStatus.RESETTING,
            SessionStatus.CLEANING,
            SessionStatus.ERROR,
        }
    ),
    SessionStatus.PASSED: frozenset(
        {SessionStatus.RESETTING, SessionStatus.CLEANING, SessionStatus.ERROR}
    ),
    SessionStatus.RESETTING: frozenset(
        {SessionStatus.READY, SessionStatus.ERROR, SessionStatus.CLEANING}
    ),
    SessionStatus.CLEANING: frozenset({SessionStatus.COMPLETED, SessionStatus.ERROR}),
    SessionStatus.ERROR: frozenset({SessionStatus.RESETTING, SessionStatus.CLEANING}),
    SessionStatus.COMPLETED: frozenset(),
}


class InvalidSessionTransition(RuntimeError):
    """Raised before persistence when a lifecycle transition is illegal."""

    code = "INVALID_SESSION_STATE"

    def __init__(self, current: SessionStatus, target: SessionStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Session cannot transition from {current.value} to {target.value}.")


class SessionStateMachine:
    """Stateless validator for the lifecycle graph documented in the TDD."""

    @staticmethod
    def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
        return target in _ALLOWED_TRANSITIONS[current]

    @classmethod
    def require_transition(cls, current: SessionStatus, target: SessionStatus) -> None:
        if not cls.can_transition(current, target):
            raise InvalidSessionTransition(current, target)


class PersistenceDto(BaseModel):
    """Immutable strict base for repository output and input DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NewLabSession(PersistenceDto):
    id: str = Field(min_length=1)
    lab_id: str = Field(min_length=1)
    variant_id: str = Field(
        default="baseline", pattern=r"^(?:baseline|variant-[a-z0-9][a-z0-9-]{0,53})$"
    )
    namespace: str = Field(pattern=r"^kubelab-[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")
    context_name: str = Field(min_length=1)
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def id_is_uuid4(cls, value: str) -> str:
        return _require_uuid4(value)


class LabSessionSnapshot(PersistenceDto):
    id: str
    lab_id: str
    variant_id: str = "baseline"
    namespace: str
    status: SessionStatus
    context_name: str
    context_fingerprint: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reset_count: int
    last_error_code: str | None
    last_error_context: dict[str, Any] | None


class SessionEventSnapshot(PersistenceDto):
    id: int
    session_id: str
    event_type: str
    from_status: SessionStatus | None
    to_status: SessionStatus | None
    context: dict[str, Any] | None
    created_at: datetime


class VerificationPurpose(StrEnum):
    INITIAL = "initial"
    SUCCESS_CONTRACT = "success_contract"
    MANUAL = "manual"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CheckResultInput(PersistenceDto):
    check_id: str = Field(min_length=1)
    check_type: str = Field(min_length=1)
    status: ValidationStatus
    expected: dict[str, Any]
    actual: dict[str, Any]
    message: str
    retryable: bool
    duration_ms: int = Field(ge=0)


class VerificationRunInput(PersistenceDto):
    id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    purpose: VerificationPurpose
    status: ValidationStatus
    reset_sequence: int = Field(ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int = Field(ge=0)
    results: tuple[CheckResultInput, ...] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def id_is_uuid4(cls, value: str) -> str:
        return _require_uuid4(value)


class RetrospectiveInput(PersistenceDto):
    symptom: str = ""
    impact: str = ""
    investigation: str = ""
    root_cause: str = ""
    resolution: str = ""
    prevention: str = ""
    interview_summary: str = ""


class RetrospectiveSnapshot(RetrospectiveInput):
    session_id: str
    updated_at: datetime


class GuidedLearningStateSnapshot(PersistenceDto):
    onboarding_completed_at: datetime | None
    last_checked_at: datetime | None
    last_environment_status: str | None
    last_environment_report: dict[str, Any] | None


class SessionEvidenceSnapshot(PersistenceDto):
    id: int
    session_id: str
    trigger: str
    capture_status: str
    summary: dict[str, Any]
    captured_at: datetime


class HintUsageSnapshot(PersistenceDto):
    level: int
    used_at: datetime
    request_count: int


class VerificationRunSnapshot(PersistenceDto):
    id: str
    session_id: str
    purpose: VerificationPurpose
    status: ValidationStatus
    checked_at: datetime
    duration_ms: int


class VerificationCheckSnapshot(PersistenceDto):
    check_id: str
    check_type: str
    status: ValidationStatus
    message: str
    retryable: bool
    duration_ms: int


class VerificationDetailSnapshot(VerificationRunSnapshot):
    results: tuple[VerificationCheckSnapshot, ...]


def _require_uuid4(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("ID must be a UUID4 string") from exc
    if parsed.version != 4:
        raise ValueError("ID must be a UUID4 string")
    return str(parsed)


__all__ = [
    "ACTIVE_SESSION_STATUSES",
    "CheckResultInput",
    "GuidedLearningStateSnapshot",
    "HintUsageSnapshot",
    "InvalidSessionTransition",
    "LabSessionSnapshot",
    "NewLabSession",
    "RetrospectiveInput",
    "RetrospectiveSnapshot",
    "SessionEventSnapshot",
    "SessionEvidenceSnapshot",
    "SessionStateMachine",
    "SessionStatus",
    "ValidationStatus",
    "VerificationPurpose",
    "VerificationCheckSnapshot",
    "VerificationDetailSnapshot",
    "VerificationRunSnapshot",
    "VerificationRunInput",
]
