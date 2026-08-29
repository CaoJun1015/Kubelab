"""Application service coordinating KubeLab session lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from kubelab.config import TrustedContext
from kubelab.context_trust import ContextTrustService, trusted_context_fingerprint
from kubelab.guided_learning import EnvironmentReadinessReport
from kubelab.kubernetes_gateway import (
    EventSummary,
    LogResult,
    NamespaceDeleteResult,
    PodSummary,
    ResourceSummary,
    SessionScope,
    WorkspaceAccess,
)
from kubelab.lab_registry import LabRegistry, LoadedLab, RegistryError
from kubelab.lab_schema import LabRequirements
from kubelab.operation_lock import OperationLock
from kubelab.repositories import (
    ActiveSessionConflict,
    SessionNotFoundError,
    SqlAlchemyUnitOfWork,
)
from kubelab.session_state import (
    LabSessionSnapshot,
    NewLabSession,
    RetrospectiveInput,
    RetrospectiveSnapshot,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
)
from kubelab.validation_engine import (
    InitialContractResult,
    ValidationGateway,
    ValidationRunResult,
)


class ManagerErrorCode(StrEnum):
    LAB_NOT_FOUND = "LAB_NOT_FOUND"
    LAB_INVALID = "LAB_INVALID"
    INITIAL_CONTRACT_FAILED = "INITIAL_CONTRACT_FAILED"
    CONTEXT_DRIFT = "CONTEXT_DRIFT"
    CLUSTER_OPERATION_FAILED = "CLUSTER_OPERATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    ENVIRONMENT_REMOVED = "ENVIRONMENT_REMOVED"


class LabManagerError(RuntimeError):
    """Sanitized application-service error with stable public context."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.context = context or {}


class ManagerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClusterState(StrEnum):
    NOT_CHECKED = "not_checked"
    PRESENT = "present"
    ABSENT = "absent"
    OWNERSHIP_MISMATCH = "ownership_mismatch"


class SessionStage(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    INVESTIGATING = "investigating"
    PASSED = "passed"
    RESETTING = "resetting"
    CLEANING = "cleaning"
    ATTENTION_REQUIRED = "attention_required"
    COMPLETED = "completed"


class SessionStatusResult(ManagerModel):
    session: LabSessionSnapshot
    namespace_exists: bool | None
    namespace_owned: bool | None
    cluster_state: ClusterState
    stage: SessionStage
    workspace_command: str = "kubelab workspace enter"


class LearningTimelineEntry(ManagerModel):
    kind: str
    title: str
    occurred_at: datetime
    status: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class SessionTimeline(ManagerModel):
    session_id: str
    entries: tuple[LearningTimelineEntry, ...]


class LabProgress(StrEnum):
    NOT_STARTED = "not_started"
    ACTIVE = "active"
    COMPLETED = "completed"


class LabCatalogItem(ManagerModel):
    id: str
    name: str
    description: str
    difficulty: str
    duration_minutes: int
    category: str
    tags: tuple[str, ...]
    progress: LabProgress


class LabCatalogResult(ManagerModel):
    labs: tuple[LabCatalogItem, ...]
    errors: tuple[RegistryError, ...]


class LabDetailResult(ManagerModel):
    lab: LabCatalogItem
    namespace: str
    task: str
    completion_description: str
    kubernetes_requirement: str
    minimum_cpu: int
    minimum_memory_mib: int
    required_addons: tuple[str, ...]
    initial_check_types: tuple[str, ...]
    success_check_types: tuple[str, ...]
    hint_count: int
    interview_questions: tuple[str, ...]


class SessionResources(ManagerModel):
    session: LabSessionSnapshot
    resources: tuple[ResourceSummary, ...]
    pods: tuple[PodSummary, ...]


class SessionEvents(ManagerModel):
    session: LabSessionSnapshot
    events: tuple[EventSummary, ...]


class HintResult(ManagerModel):
    session_id: str
    lab_id: str
    level: int
    total_levels: int
    content: str
    newly_unlocked: bool


class RetrospectiveEditState(ManagerModel):
    session: LabSessionSnapshot
    retrospective: RetrospectiveSnapshot | None


class ValidationService(Protocol):
    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
    ) -> InitialContractResult: ...

    def validate_success_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
        purpose: VerificationPurpose = VerificationPurpose.MANUAL,
    ) -> ValidationRunResult: ...


class ReadinessGuard(Protocol):
    def assert_ready(self, requirements: LabRequirements) -> EnvironmentReadinessReport: ...


class ClusterGateway(ValidationGateway, Protocol):
    def create_environment(self, scope: SessionScope) -> None: ...

    def apply_lab(self, scope: SessionScope, loaded: LoadedLab, registry: LabRegistry) -> None: ...

    def namespace_exists(self, scope: SessionScope) -> bool: ...

    def assert_namespace_owned(self, scope: SessionScope) -> None: ...

    def provision_workspace(self, scope: SessionScope) -> WorkspaceAccess: ...

    def revoke_workspace(self, scope: SessionScope) -> None: ...

    def delete_environment(
        self, scope: SessionScope, *, wait_timeout_seconds: float = 120
    ) -> NamespaceDeleteResult: ...

    def list_resources(self, scope: SessionScope) -> tuple[ResourceSummary, ...]: ...

    def list_pods(self, scope: SessionScope) -> tuple[PodSummary, ...]: ...

    def list_events(self, scope: SessionScope) -> tuple[EventSummary, ...]: ...

    def read_logs(
        self,
        scope: SessionScope,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 200,
    ) -> LogResult: ...

    def close(self) -> None: ...


class GatewayFactory(Protocol):
    def __call__(self, trusted: TrustedContext, context_fingerprint: str) -> ClusterGateway: ...


ClusterReadResult = TypeVar("ClusterReadResult")


class LabManager:
    """Coordinate short transactions around potentially slow cluster operations."""

    def __init__(
        self,
        *,
        registry: LabRegistry,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
        operation_lock: OperationLock,
        context_trust: ContextTrustService,
        gateway_factory: GatewayFactory,
        validation: ValidationService,
        readiness: ReadinessGuard | None = None,
    ) -> None:
        self._registry = registry
        self._unit_of_work = unit_of_work
        self._operation_lock = operation_lock
        self._context_trust = context_trust
        self._gateway_factory = gateway_factory
        self._validation = validation
        self._readiness = readiness

    def list_labs(
        self, *, category: str | None = None, progress: LabProgress | None = None
    ) -> LabCatalogResult:
        """Return the safe lab catalogue with local learner progress."""
        snapshot = self._registry.scan()
        with self._unit_of_work() as uow:
            active = uow.sessions.get_active()
            passed = uow.sessions.passed_lab_ids()
        items = tuple(
            self._catalog_item(lab, active=active, passed=passed)
            for lab in snapshot.labs
            if category is None or lab.definition.metadata.category == category
        )
        if progress is not None:
            items = tuple(item for item in items if item.progress is progress)
        return LabCatalogResult(labs=items, errors=snapshot.errors)

    def show_lab(self, lab_id: str) -> LabDetailResult:
        """Show a lab brief without revealing checks' answers or hint content."""
        loaded = self._require_lab(lab_id)
        with self._unit_of_work() as uow:
            active = uow.sessions.get_active()
            passed = uow.sessions.passed_lab_ids()
        definition = loaded.definition
        return LabDetailResult(
            lab=self._catalog_item(loaded, active=active, passed=passed),
            namespace=definition.environment.namespace,
            task=definition.task.description,
            completion_description=definition.task.completion_description,
            kubernetes_requirement=definition.requirements.kubernetes,
            minimum_cpu=definition.requirements.minimum_cpu,
            minimum_memory_mib=definition.requirements.minimum_memory_mib,
            required_addons=definition.requirements.addons,
            initial_check_types=tuple(check.type for check in definition.initial_checks),
            success_check_types=tuple(check.type for check in definition.success_checks),
            hint_count=len(definition.hints),
            interview_questions=definition.interview.questions,
        )

    def session_snapshot(
        self, session_id: str | None = None, *, latest_if_inactive: bool = False
    ) -> LabSessionSnapshot:
        """Read a Session without reconciling or contacting the cluster."""
        if session_id is not None:
            return self._require_session(session_id, allow_completed=True)
        with self._unit_of_work() as uow:
            session = uow.sessions.get_active()
            if session is None and latest_if_inactive:
                session = uow.sessions.get_latest()
        if session is None:
            raise LabManagerError(
                ManagerErrorCode.SESSION_NOT_FOUND, "The requested Session was not found."
            )
        return session

    def session_status_snapshot(self, session_id: str | None = None) -> SessionStatusResult:
        """Restore a Session from SQLite without touching trust or the cluster."""
        session = self.session_snapshot(session_id)
        return SessionStatusResult(
            session=session,
            namespace_exists=None,
            namespace_owned=None,
            cluster_state=ClusterState.NOT_CHECKED,
            stage=_session_stage(session.status),
        )

    def timeline(self, session_id: str | None = None) -> SessionTimeline:
        """Merge persisted learning activity without exposing validation internals."""
        session = self.session_snapshot(session_id, latest_if_inactive=True)
        with self._unit_of_work() as uow:
            events = uow.sessions.list_events(session.id)
            hints = uow.hints.list_for_session(session.id)
            verifications = uow.verifications.list_for_session(session.id)
            evidence = uow.guided_learning.list_evidence(session.id)
        entries = [
            LearningTimelineEntry(
                kind="session_event",
                title=_event_title(event.event_type),
                occurred_at=event.created_at,
                status=event.to_status.value if event.to_status is not None else None,
            )
            for event in events
        ]
        entries.extend(
            LearningTimelineEntry(
                kind="hint",
                title=f"解锁第 {hint.level} 层提示",
                occurred_at=hint.used_at,
                details={"level": hint.level, "request_count": hint.request_count},
            )
            for hint in hints
        )
        entries.extend(
            LearningTimelineEntry(
                kind="verification",
                title="手动验证" if run.purpose is VerificationPurpose.MANUAL else "契约检查",
                occurred_at=run.checked_at,
                status=(
                    "unavailable" if run.status is ValidationStatus.ERROR else run.status.value
                ),
                details={"duration_ms": run.duration_ms},
            )
            for run in verifications
        )
        entries.extend(
            LearningTimelineEntry(
                kind="evidence",
                title=_evidence_title(item.trigger),
                occurred_at=item.captured_at,
                status=item.capture_status,
                details=item.summary,
            )
            for item in evidence
        )
        entries.sort(key=lambda item: (item.occurred_at, item.kind, item.title))
        return SessionTimeline(session_id=session.id, entries=tuple(entries))

    def resources(self, session_id: str | None = None) -> SessionResources:
        """Return safe resource and Pod summaries for the owned experiment Namespace."""
        session, payload = self._cluster_read(
            session_id,
            operation="resources",
            reader=lambda gateway, scope: (
                gateway.list_resources(scope),
                gateway.list_pods(scope),
            ),
        )
        resources, pods = payload
        return SessionResources(session=session, resources=resources, pods=pods)

    def events(self, session_id: str | None = None) -> SessionEvents:
        """Return Kubernetes Events from the owned experiment Namespace."""
        session, events = self._cluster_read(
            session_id,
            operation="events",
            reader=lambda gateway, scope: gateway.list_events(scope),
        )
        return SessionEvents(session=session, events=events)

    def logs(
        self,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 200,
        session_id: str | None = None,
    ) -> LogResult:
        """Read bounded logs from one Pod in the owned experiment Namespace."""
        _, result = self._cluster_read(
            session_id,
            operation="logs",
            reader=lambda gateway, scope: gateway.read_logs(
                scope,
                pod,
                container=container,
                previous=previous,
                tail_lines=tail_lines,
            ),
        )
        return result

    def open_workspace(self, session_id: str | None = None) -> WorkspaceAccess:
        """Issue short-lived namespace-only credentials for an interactive shell."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status not in {
                SessionStatus.READY,
                SessionStatus.IN_PROGRESS,
                SessionStatus.PASSED,
            }:
                raise LabManagerError(
                    "INVALID_SESSION_STATE",
                    "The workspace is unavailable in the current Session state.",
                )
            trusted, fingerprint = self._trusted_for_session(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                access = gateway.provision_workspace(self._scope(session))
                if session.status is SessionStatus.READY:
                    self._transition(
                        session.id,
                        SessionStatus.IN_PROGRESS,
                        event_type="workspace_entered",
                    )
                return access
            except Exception as exc:
                raise self._manager_error(exc, operation="workspace") from exc
            finally:
                gateway.close()

    def close_workspace(self, session_id: str) -> None:
        """Revoke ephemeral workspace RBAC when its shell exits."""
        with self._operation_lock:
            session = self._require_session(session_id, allow_completed=True)
            if session.status is SessionStatus.COMPLETED:
                return
            trusted, fingerprint = self._trusted_for_session(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                scope = self._scope(session)
                if gateway.namespace_exists(scope):
                    gateway.revoke_workspace(scope)
            except Exception as exc:
                raise self._manager_error(exc, operation="workspace cleanup") from exc
            finally:
                gateway.close()

    def next_hint(self, session_id: str | None = None) -> HintResult:
        """Reveal one hint level at a time and record first use."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status not in {
                SessionStatus.READY,
                SessionStatus.IN_PROGRESS,
                SessionStatus.PASSED,
            }:
                raise LabManagerError(
                    "INVALID_SESSION_STATE",
                    "Hints are unavailable in the current Session state.",
                )
            self._trusted_for_session(session)
            lab = self._require_lab(session.lab_id)
            if session.status is SessionStatus.READY:
                session = self._transition(
                    session.id,
                    SessionStatus.IN_PROGRESS,
                    event_type="hint_requested",
                )
            with self._unit_of_work() as uow:
                used = uow.hints.used_levels(session.id)
                level = min(len(used) + 1, len(lab.definition.hints))
                newly_unlocked = uow.hints.record_once(session.id, level)
                uow.commit()
            hint = next(item for item in lab.definition.hints if item.level == level)
            return HintResult(
                session_id=session.id,
                lab_id=session.lab_id,
                level=level,
                total_levels=len(lab.definition.hints),
                content=hint.content,
                newly_unlocked=newly_unlocked,
            )

    def retrospective(self, session_id: str | None = None) -> RetrospectiveEditState:
        """Load the active or latest Session retrospective for CLI editing."""
        session = self.session_snapshot(session_id, latest_if_inactive=True)
        with self._unit_of_work() as uow:
            retrospective = uow.retrospectives.get(session.id)
        return RetrospectiveEditState(session=session, retrospective=retrospective)

    def save_retrospective(
        self, value: RetrospectiveInput, session_id: str | None = None
    ) -> RetrospectiveSnapshot:
        """Save the active or latest Session retrospective in one short transaction."""
        with self._operation_lock:
            session = self.session_snapshot(session_id, latest_if_inactive=True)
            with self._unit_of_work() as uow:
                result = uow.retrospectives.save(session.id, value)
                uow.commit()
                return result

    def start(self, lab_id: str) -> LabSessionSnapshot:
        """Provision one lab and prove its initial fault contract before returning ready."""
        with self._operation_lock:
            lab = self._require_lab(lab_id)
            if self._readiness is not None:
                self._readiness.assert_ready(lab.definition.requirements)
            trusted = self._context_trust.assert_trusted_context()
            fingerprint = trusted_context_fingerprint(trusted)
            with self._unit_of_work() as uow:
                active = uow.sessions.get_active()
                if active is not None:
                    raise ActiveSessionConflict(active)
                session = uow.sessions.create(
                    NewLabSession(
                        id=str(uuid4()),
                        lab_id=lab_id,
                        namespace=lab.definition.environment.namespace,
                        context_name=trusted.name,
                        context_fingerprint=fingerprint,
                    )
                )
                uow.commit()

            scope = self._scope(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                gateway.create_environment(scope)
                gateway.apply_lab(scope, lab, self._registry)
                result = self._validation.validate_initial_contract(
                    scope, lab, gateway, reset_sequence=0
                )
                if result.status is not ValidationStatus.PASSED:
                    raise LabManagerError(
                        result.error_code or ManagerErrorCode.INITIAL_CONTRACT_FAILED,
                        "The lab initial fault contract was not satisfied.",
                        retryable=result.retryable,
                    )
                ready = self._transition(
                    session.id,
                    SessionStatus.READY,
                    event_type="environment_ready",
                    context={"namespace": session.namespace},
                )
                self._capture_evidence(ready, gateway, trigger="environment_ready")
                return ready
            except Exception as exc:
                self._rollback_failed_environment(session, gateway, exc, operation="start")
                raise self._manager_error(exc, operation="start") from exc
            finally:
                gateway.close()

    def status(self, session_id: str | None = None) -> SessionStatusResult:
        """Reconcile one Session with its owned Namespace without taking over orphans."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status is SessionStatus.COMPLETED:
                return SessionStatusResult(
                    session=session,
                    namespace_exists=False,
                    namespace_owned=False,
                    cluster_state=ClusterState.ABSENT,
                    stage=_session_stage(session.status),
                )
            trusted, fingerprint = self._trusted_for_session(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                if not gateway.namespace_exists(self._scope(session)):
                    completed = self._complete_removed_environment(session)
                    return SessionStatusResult(
                        session=completed,
                        namespace_exists=False,
                        namespace_owned=False,
                        cluster_state=ClusterState.ABSENT,
                        stage=_session_stage(completed.status),
                    )
                try:
                    gateway.assert_namespace_owned(self._scope(session))
                except Exception as exc:
                    self._mark_error(
                        session,
                        exc,
                        event_type="namespace_identity_mismatch",
                        operation="status",
                    )
                    raise self._manager_error(exc, operation="status") from exc
                return SessionStatusResult(
                    session=session,
                    namespace_exists=True,
                    namespace_owned=True,
                    cluster_state=ClusterState.PRESENT,
                    stage=_session_stage(session.status),
                )
            finally:
                gateway.close()

    def reset(self, session_id: str | None = None) -> LabSessionSnapshot:
        """Safely delete and recreate a Session environment while preserving its ID."""
        with self._operation_lock:
            session = self._require_session(session_id)
            if session.status is SessionStatus.COMPLETED:
                raise LabManagerError(
                    "INVALID_SESSION_STATE", "A completed Session cannot be reset."
                )
            lab = self._require_lab(session.lab_id)
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is not SessionStatus.RESETTING:
                session = self._transition(
                    session.id, SessionStatus.RESETTING, event_type="reset_started"
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                gateway.delete_environment(self._scope(session))
                gateway.create_environment(self._scope(session))
                gateway.apply_lab(self._scope(session), lab, self._registry)
                result = self._validation.validate_initial_contract(
                    self._scope(session),
                    lab,
                    gateway,
                    reset_sequence=session.reset_count + 1,
                )
                if result.status is not ValidationStatus.PASSED:
                    raise LabManagerError(
                        result.error_code or ManagerErrorCode.INITIAL_CONTRACT_FAILED,
                        "The reset environment did not satisfy its initial fault contract.",
                        retryable=result.retryable,
                    )
                with self._unit_of_work() as uow:
                    uow.sessions.increment_reset_count(session.id)
                    ready = uow.sessions.transition(
                        session.id,
                        SessionStatus.READY,
                        event_type="reset_completed",
                    )
                    uow.commit()
                self._capture_evidence(ready, gateway, trigger="reset_completed")
                return ready
            except Exception as exc:
                self._rollback_reset(session, gateway, exc)
                raise self._manager_error(exc, operation="reset") from exc
            finally:
                gateway.close()

    def cleanup(self, session_id: str | None = None) -> LabSessionSnapshot:
        """Idempotently clean any active Session through exact Namespace ownership checks."""
        with self._operation_lock:
            session = self._require_session(session_id, allow_completed=True)
            if session.status is SessionStatus.COMPLETED:
                return session
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is not SessionStatus.CLEANING:
                session = self._transition(
                    session.id, SessionStatus.CLEANING, event_type="cleanup_started"
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                self._capture_evidence(session, gateway, trigger="cleanup_before_delete")
                result = gateway.delete_environment(self._scope(session))
                completed = self._transition(
                    session.id,
                    SessionStatus.COMPLETED,
                    event_type="cleanup_completed",
                    context={"already_absent": result.already_absent},
                )
                self._record_absent_evidence(completed, trigger="cleanup_completed")
                return completed
            except Exception as exc:
                self._mark_error(
                    session,
                    exc,
                    event_type="cleanup_failed",
                    operation="cleanup",
                )
                raise self._manager_error(exc, operation="cleanup") from exc
            finally:
                gateway.close()

    def verify(self, session_id: str | None = None) -> ValidationRunResult:
        """Run successChecks and advance an in-progress Session only on success."""
        with self._operation_lock:
            session = self._require_session(session_id, allow_completed=True)
            if session.status not in {
                SessionStatus.READY,
                SessionStatus.IN_PROGRESS,
                SessionStatus.PASSED,
            }:
                raise LabManagerError(
                    "INVALID_SESSION_STATE",
                    "The Session cannot be verified in its current state.",
                )
            lab = self._require_lab(session.lab_id)
            trusted, fingerprint = self._trusted_for_session(session)
            if session.status is SessionStatus.READY:
                session = self._transition(
                    session.id,
                    SessionStatus.IN_PROGRESS,
                    event_type="verification_started",
                )
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                result = self._validation.validate_success_contract(
                    self._scope(session),
                    lab,
                    gateway,
                    reset_sequence=session.reset_count,
                    purpose=VerificationPurpose.MANUAL,
                )
                self._capture_evidence(session, gateway, trigger="manual_verify")
                if (
                    result.status is ValidationStatus.PASSED
                    and session.status is SessionStatus.IN_PROGRESS
                ):
                    self._transition(
                        session.id,
                        SessionStatus.PASSED,
                        event_type="success_contract_passed",
                        context={"verification_run_id": result.id},
                    )
                return result
            finally:
                gateway.close()

    @staticmethod
    def _catalog_item(
        loaded: LoadedLab,
        *,
        active: LabSessionSnapshot | None,
        passed: frozenset[str],
    ) -> LabCatalogItem:
        metadata = loaded.definition.metadata
        if active is not None and active.lab_id == metadata.id:
            progress = LabProgress.ACTIVE
        elif metadata.id in passed:
            progress = LabProgress.COMPLETED
        else:
            progress = LabProgress.NOT_STARTED
        return LabCatalogItem(
            id=metadata.id,
            name=metadata.name,
            description=metadata.description,
            difficulty=metadata.difficulty,
            duration_minutes=metadata.duration_minutes,
            category=metadata.category,
            tags=metadata.tags,
            progress=progress,
        )

    def _cluster_read(
        self,
        session_id: str | None,
        *,
        operation: str,
        reader: Callable[[ClusterGateway, SessionScope], ClusterReadResult],
    ) -> tuple[LabSessionSnapshot, ClusterReadResult]:
        with self._operation_lock:
            session = self._require_session(session_id)
            trusted, fingerprint = self._trusted_for_session(session)
            gateway = self._gateway_factory(trusted, fingerprint)
            try:
                scope = self._scope(session)
                if not gateway.namespace_exists(scope):
                    raise LabManagerError(
                        ManagerErrorCode.ENVIRONMENT_REMOVED,
                        "The experiment Namespace no longer exists.",
                    )
                try:
                    gateway.assert_namespace_owned(scope)
                except Exception:
                    raise
                return session, reader(gateway, scope)
            except LabManagerError:
                raise
            except Exception as exc:
                raise self._manager_error(exc, operation=operation) from exc
            finally:
                gateway.close()

    def _require_lab(self, lab_id: str) -> LoadedLab:
        snapshot = self._registry.scan()
        match = next((lab for lab in snapshot.labs if lab.definition.metadata.id == lab_id), None)
        if match is not None:
            return match
        related = tuple(error for error in snapshot.errors if error.lab_id == lab_id)
        if related:
            raise LabManagerError(
                ManagerErrorCode.LAB_INVALID,
                "The requested lab is invalid.",
                context={"errors": tuple(_registry_error_context(error) for error in related)},
            )
        raise LabManagerError(ManagerErrorCode.LAB_NOT_FOUND, "The requested lab was not found.")

    def _require_session(
        self, session_id: str | None, *, allow_completed: bool = False
    ) -> LabSessionSnapshot:
        try:
            with self._unit_of_work() as uow:
                session = (
                    uow.sessions.require(session_id)
                    if session_id is not None
                    else uow.sessions.get_active()
                )
                if session is None:
                    raise SessionNotFoundError("There is no active Session.")
                if session.status is SessionStatus.COMPLETED and not allow_completed:
                    raise SessionNotFoundError("There is no active Session.")
                return session
        except SessionNotFoundError as exc:
            raise LabManagerError(
                ManagerErrorCode.SESSION_NOT_FOUND, "The requested Session was not found."
            ) from exc

    def _trusted_for_session(self, session: LabSessionSnapshot) -> tuple[TrustedContext, str]:
        try:
            trusted = self._context_trust.assert_trusted_context()
        except Exception as exc:
            raise LabManagerError(
                ManagerErrorCode.CONTEXT_DRIFT,
                "The current Context no longer matches the Session.",
            ) from exc
        fingerprint = trusted_context_fingerprint(trusted)
        if trusted.name != session.context_name or fingerprint != session.context_fingerprint:
            raise LabManagerError(
                ManagerErrorCode.CONTEXT_DRIFT,
                "The current Context no longer matches the Session.",
            )
        return trusted, fingerprint

    @staticmethod
    def _scope(session: LabSessionSnapshot) -> SessionScope:
        return SessionScope(
            lab_id=session.lab_id,
            session_id=session.id,
            namespace=session.namespace,
            context_fingerprint=session.context_fingerprint,
        )

    def _transition(
        self,
        session_id: str,
        target: SessionStatus,
        *,
        event_type: str,
        context: dict[str, Any] | None = None,
    ) -> LabSessionSnapshot:
        with self._unit_of_work() as uow:
            result = uow.sessions.transition(
                session_id, target, event_type=event_type, context=context
            )
            uow.commit()
            return result

    def _capture_evidence(
        self, session: LabSessionSnapshot, gateway: ClusterGateway, *, trigger: str
    ) -> None:
        """Best-effort public resource summary; never changes the parent operation result."""
        try:
            resources = gateway.list_resources(self._scope(session))
            pods = gateway.list_pods(self._scope(session))
            allowed_kinds = {
                "ConfigMap",
                "CronJob",
                "DaemonSet",
                "Deployment",
                "Job",
                "PersistentVolumeClaim",
                "Pod",
                "ReplicaSet",
                "Service",
                "StatefulSet",
            }
            resource_counts: dict[str, int] = {}
            for resource in resources:
                if resource.kind in allowed_kinds:
                    resource_counts[resource.kind] = resource_counts.get(resource.kind, 0) + 1
            phases: dict[str, int] = {}
            for pod in pods:
                phase = pod.phase or "Unknown"
                phases[phase] = phases.get(phase, 0) + 1
            summary: dict[str, Any] = {
                "resource_counts": resource_counts,
                "pods": {
                    "total": len(pods),
                    "ready": sum(pod.ready for pod in pods),
                    "restarts": sum(pod.restart_count for pod in pods),
                    "phases": phases,
                },
            }
            capture_status = "captured"
        except Exception:
            summary = {"reason": "RESOURCE_SNAPSHOT_UNAVAILABLE"}
            capture_status = "unavailable"
        try:
            with self._unit_of_work() as uow:
                uow.guided_learning.add_evidence(
                    session.id,
                    trigger=trigger,
                    capture_status=capture_status,
                    summary=summary,
                )
                uow.commit()
        except Exception:
            return

    def _record_absent_evidence(self, session: LabSessionSnapshot, *, trigger: str) -> None:
        try:
            with self._unit_of_work() as uow:
                uow.guided_learning.add_evidence(
                    session.id,
                    trigger=trigger,
                    capture_status="captured",
                    summary={"namespace_state": "absent"},
                )
                uow.commit()
        except Exception:
            return

    def _complete_removed_environment(self, session: LabSessionSnapshot) -> LabSessionSnapshot:
        current = session
        if current.status is not SessionStatus.CLEANING:
            current = self._transition(
                current.id,
                SessionStatus.CLEANING,
                event_type="environment_removed_externally",
            )
        return self._transition(
            current.id,
            SessionStatus.COMPLETED,
            event_type="external_removal_reconciled",
        )

    def _rollback_failed_environment(
        self,
        session: LabSessionSnapshot,
        gateway: ClusterGateway,
        cause: Exception,
        *,
        operation: str,
    ) -> None:
        cleaning = self._transition(
            session.id,
            SessionStatus.CLEANING,
            event_type=f"{operation}_failed",
            context={"error_code": _error_code(cause)},
        )
        try:
            gateway.delete_environment(self._scope(cleaning))
        except Exception as cleanup_error:
            self._mark_error(
                cleaning,
                cleanup_error,
                event_type="rollback_cleanup_failed",
                operation=operation,
            )
            return
        self._transition(
            cleaning.id,
            SessionStatus.COMPLETED,
            event_type="failed_environment_removed",
            context={"error_code": _error_code(cause)},
        )

    def _rollback_reset(
        self, session: LabSessionSnapshot, gateway: ClusterGateway, cause: Exception
    ) -> None:
        cleaning = self._transition(
            session.id,
            SessionStatus.CLEANING,
            event_type="reset_failed",
            context={"error_code": _error_code(cause)},
        )
        try:
            gateway.delete_environment(self._scope(cleaning))
        except Exception as cleanup_error:
            self._mark_error(
                cleaning,
                cleanup_error,
                event_type="reset_cleanup_failed",
                operation="reset",
            )
            return
        self._mark_error(
            cleaning,
            cause,
            event_type="reset_environment_removed",
            operation="reset",
        )

    def _mark_error(
        self,
        session: LabSessionSnapshot,
        cause: Exception,
        *,
        event_type: str,
        operation: str,
    ) -> LabSessionSnapshot:
        if session.status is SessionStatus.ERROR:
            return session
        with self._unit_of_work() as uow:
            failed = uow.sessions.transition(
                session.id,
                SessionStatus.ERROR,
                event_type=event_type,
                error_code=_error_code(cause),
                error_context={"operation": operation},
            )
            uow.commit()
            return failed

    @staticmethod
    def _manager_error(error: Exception, *, operation: str) -> LabManagerError:
        if isinstance(error, LabManagerError):
            return error
        retryable = bool(getattr(error, "retryable", False))
        code = str(getattr(error, "code", ManagerErrorCode.CLUSTER_OPERATION_FAILED))
        return LabManagerError(
            code,
            f"The lab {operation} operation failed.",
            retryable=retryable,
            context={"operation": operation},
        )


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", ManagerErrorCode.CLUSTER_OPERATION_FAILED)
    return str(code)


def _session_stage(status: SessionStatus) -> SessionStage:
    return {
        SessionStatus.PROVISIONING: SessionStage.PREPARING,
        SessionStatus.READY: SessionStage.READY,
        SessionStatus.IN_PROGRESS: SessionStage.INVESTIGATING,
        SessionStatus.PASSED: SessionStage.PASSED,
        SessionStatus.RESETTING: SessionStage.RESETTING,
        SessionStatus.CLEANING: SessionStage.CLEANING,
        SessionStatus.ERROR: SessionStage.ATTENTION_REQUIRED,
        SessionStatus.COMPLETED: SessionStage.COMPLETED,
    }[status]


def _event_title(event_type: str) -> str:
    return {
        "session_created": "Session 已创建",
        "environment_ready": "实验环境已就绪",
        "workspace_entered": "进入受限 Workspace",
        "hint_requested": "开始使用提示",
        "verification_started": "开始手动验证",
        "success_contract_passed": "成功契约已通过",
        "reset_started": "开始重置实验",
        "reset_completed": "实验已重置",
        "cleanup_started": "开始清理实验",
        "cleanup_completed": "实验已清理",
        "environment_removed_externally": "检测到环境已移除",
        "external_removal_reconciled": "外部移除已协调",
    }.get(event_type, "Session 状态已更新")


def _evidence_title(trigger: str) -> str:
    return {
        "environment_ready": "就绪资源摘要",
        "reset_completed": "重置后资源摘要",
        "manual_verify": "验证时资源摘要",
        "cleanup_before_delete": "清理前资源摘要",
        "cleanup_completed": "清理完成摘要",
    }.get(trigger, "资源状态摘要")


def _registry_error_context(error: RegistryError) -> dict[str, Any]:
    return {
        "code": error.code.value,
        "lab_path": error.lab_path,
        "field_path": error.field_path,
        "retryable": error.retryable,
    }


__all__ = [
    "ClusterState",
    "ClusterGateway",
    "GatewayFactory",
    "HintResult",
    "InitialContractResult",
    "LabCatalogItem",
    "LabCatalogResult",
    "LabDetailResult",
    "LabManager",
    "LabManagerError",
    "LearningTimelineEntry",
    "LabProgress",
    "ManagerErrorCode",
    "RetrospectiveEditState",
    "SessionEvents",
    "SessionResources",
    "SessionStage",
    "SessionStatusResult",
    "SessionTimeline",
    "ValidationService",
    "WorkspaceAccess",
]
