"""Application service coordinating KubeLab session lifecycle operations."""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from kubelab.config import TrustedContext
from kubelab.context_trust import ContextTrustService, trusted_context_fingerprint
from kubelab.guided_learning import EnvironmentReadinessReport, public_validation_outcome
from kubelab.kubernetes_gateway import (
    EventSummary,
    LogResult,
    NamespaceDeleteResult,
    PodSummary,
    ResourceSummary,
    SessionScope,
    WorkspaceAccess,
)
from kubelab.lab_registry import (
    EffectiveLab,
    ExecutableLab,
    LabMaterializationError,
    LabRegistry,
    LabVariantNotFoundError,
    LoadedLab,
    LoadedVariant,
    RegistryError,
)
from kubelab.lab_schema import LabRequirements
from kubelab.learning_paths import (
    AfterKnowledgeCard,
    BeforeKnowledgeCard,
    LabLearningFacts,
    LearningFacts,
    LearningPathCatalogDefinition,
    LearningPathCatalogReport,
    LearningPathDefinition,
    LearningPathDetail,
    LearningPathOutcome,
    LearningPathRegistry,
    LearningRecommendation,
    SymptomCatalog,
    derive_outcome,
    evaluate_path,
    recommend_next,
    render_outcome_markdown,
)
from kubelab.operation_lock import OperationLock
from kubelab.redaction import redact_json
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
    SessionEventSnapshot,
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
    LAB_VARIANT_NOT_FOUND = "LAB_VARIANT_NOT_FOUND"
    LEARNING_PATH_INVALID = "LEARNING_PATH_INVALID"
    LEARNING_PATH_NOT_FOUND = "LEARNING_PATH_NOT_FOUND"


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


class PracticeMode(StrEnum):
    BASELINE = "baseline"
    BLIND_REPEAT = "blind_repeat"


class SessionStatusResult(ManagerModel):
    session: LabSessionSnapshot
    namespace_exists: bool | None
    namespace_owned: bool | None
    cluster_state: ClusterState
    stage: SessionStage
    practice_mode: PracticeMode = PracticeMode.BASELINE
    scenario_revealed: bool = True
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
    baseline_completed: bool = False
    variant_total: int = 0
    variant_completed: int = 0


class FaultMapEntry(ManagerModel):
    slot: int
    revealed: bool
    name: str | None = None
    description: str | None = None
    key_evidence: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    prevention: str | None = None


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
    practice_mode: PracticeMode = PracticeMode.BASELINE
    scenario_revealed: bool = True
    scenario_name: str | None = None
    scenario_description: str | None = None
    key_evidence: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    prevention: str | None = None
    fault_map: tuple[FaultMapEntry, ...] = ()
    learning_path_ids: tuple[str, ...] = ()
    knowledge_before: BeforeKnowledgeCard | None = None
    knowledge_after: AfterKnowledgeCard | None = None


class SessionResources(ManagerModel):
    session: LabSessionSnapshot
    resources: tuple[ResourceSummary, ...]
    pods: tuple[PodSummary, ...]


class SessionEvents(ManagerModel):
    session: LabSessionSnapshot
    events: tuple[EventSummary, ...]


class HintKind(StrEnum):
    OBSERVATION = "observation"
    COMMAND = "command"
    FAULT_DIRECTION = "fault_direction"


class HintResult(ManagerModel):
    session_id: str
    lab_id: str
    level: int
    total_levels: int
    content: str
    newly_unlocked: bool
    kind: HintKind
    request_count: int
    unlocked_count: int


class PublicVerificationCheckSummary(ManagerModel):
    check_id: str
    check_type: str
    status: str
    message: str
    retryable: bool
    duration_ms: int


class PublicVerificationSummary(ManagerModel):
    status: str
    checked_at: datetime
    duration_ms: int
    results: tuple[PublicVerificationCheckSummary, ...]


class RetrospectiveMetadata(ManagerModel):
    lab_id: str
    lab_name: str
    category: str
    difficulty: str
    session_id: str
    namespace: str
    started_at: datetime | None
    first_passed_at: datetime | None
    completed_at: datetime | None
    hint_request_count: int
    unlocked_hint_count: int
    manual_verification_count: int
    reset_count: int
    completion_duration_seconds: int | None
    last_verification: PublicVerificationSummary | None
    practice_mode: PracticeMode = PracticeMode.BASELINE
    scenario_name: str | None = None
    scenario_description: str | None = None
    key_evidence: str | None = None
    scenario_root_cause: str | None = None
    scenario_resolution: str | None = None
    scenario_prevention: str | None = None


class RetrospectiveEditState(ManagerModel):
    session: LabSessionSnapshot
    retrospective: RetrospectiveSnapshot | None
    metadata: RetrospectiveMetadata | None = None


class LabLearningProgress(ManagerModel):
    lab_id: str
    name: str
    category: str
    attempt_count: int
    completion_count: int
    repeat_completion_count: int
    first_completed_at: datetime | None
    last_completed_at: datetime | None
    baseline_completed: bool = False
    variant_total: int = 0
    variant_completed: int = 0
    variant_attempt_count: int = 0
    last_practiced_at: datetime | None = None
    revealed_scenarios: tuple[str, ...] = ()


class CategoryLearningProgress(ManagerModel):
    category: str
    lab_count: int
    completed_lab_count: int
    attempt_count: int


class LearningProgressReport(ManagerModel):
    labs: tuple[LabLearningProgress, ...]
    categories: tuple[CategoryLearningProgress, ...]


class ValidationService(Protocol):
    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: ExecutableLab,
        gateway: ValidationGateway,
        reset_sequence: int,
    ) -> InitialContractResult: ...

    def validate_success_contract(
        self,
        scope: SessionScope,
        lab: ExecutableLab,
        gateway: ValidationGateway,
        reset_sequence: int,
        purpose: VerificationPurpose = VerificationPurpose.MANUAL,
    ) -> ValidationRunResult: ...


class ReadinessGuard(Protocol):
    def assert_ready(self, requirements: LabRequirements) -> EnvironmentReadinessReport: ...


class ClusterGateway(ValidationGateway, Protocol):
    def create_environment(self, scope: SessionScope) -> None: ...

    def apply_lab(
        self, scope: SessionScope, loaded: ExecutableLab, registry: LabRegistry
    ) -> None: ...

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
        learning_paths: LearningPathRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._unit_of_work = unit_of_work
        self._operation_lock = operation_lock
        self._context_trust = context_trust
        self._gateway_factory = gateway_factory
        self._validation = validation
        self._readiness = readiness
        self._learning_path_registry = learning_paths

    def list_labs(
        self, *, category: str | None = None, progress: LabProgress | None = None
    ) -> LabCatalogResult:
        """Return the safe lab catalogue with local learner progress."""
        snapshot = self._registry.scan()
        with self._unit_of_work() as uow:
            active = uow.sessions.get_active()
            passed = uow.sessions.passed_lab_ids()
            sessions = uow.sessions.list_all()
            event_map = {session.id: uow.sessions.list_events(session.id) for session in sessions}
        items = tuple(
            self._catalog_item(
                lab,
                active=active,
                passed=passed,
                sessions=sessions,
                event_map=event_map,
            )
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
            sessions = uow.sessions.list_all()
            event_map = {session.id: uow.sessions.list_events(session.id) for session in sessions}
        definition = loaded.definition
        effective: ExecutableLab = loaded
        practice_mode = PracticeMode.BASELINE
        scenario_revealed = True
        active_variant = None
        if active is not None and active.lab_id == lab_id and active.variant_id != "baseline":
            effective = self._resolve_session_lab(active, loaded=loaded)
            assert isinstance(effective, EffectiveLab)
            active_variant = effective.variant
            practice_mode = PracticeMode.BLIND_REPEAT
            scenario_revealed = _has_passed(event_map.get(active.id, ()))
        public_definition = effective.definition
        passed_variants = _passed_variants(loaded.definition.metadata.id, sessions, event_map)
        fault_map = tuple(
            _fault_map_entry(
                slot=index,
                variant=variant,
                revealed=variant.definition.metadata.id in passed_variants,
            )
            for index, variant in enumerate(loaded.variants, start=1)
        )
        reveal = active_variant.definition.reveal if active_variant and scenario_revealed else None
        learning_path_ids: tuple[str, ...] = ()
        knowledge_before = None
        knowledge_after = None
        learning_catalog = self._optional_learning_catalog()
        if learning_catalog is not None:
            learning_path_ids = tuple(
                path.metadata.id
                for path in learning_catalog.paths
                if any(node.lab_id == lab_id for node in path.nodes)
            )
            card = next(
                (card for card in learning_catalog.knowledge_cards if card.lab_id == lab_id),
                None,
            )
            if card is not None:
                knowledge_before = card.before
                if "baseline" in passed_variants:
                    knowledge_after = card.after
        return LabDetailResult(
            lab=self._catalog_item(
                loaded,
                active=active,
                passed=passed,
                sessions=sessions,
                event_map=event_map,
            ),
            namespace=definition.environment.namespace,
            task=public_definition.task.description,
            completion_description=public_definition.task.completion_description,
            kubernetes_requirement=definition.requirements.kubernetes,
            minimum_cpu=definition.requirements.minimum_cpu,
            minimum_memory_mib=definition.requirements.minimum_memory_mib,
            required_addons=definition.requirements.addons,
            initial_check_types=(
                ()
                if practice_mode is PracticeMode.BLIND_REPEAT and not scenario_revealed
                else tuple(check.type for check in public_definition.initial_checks)
            ),
            success_check_types=(
                ()
                if practice_mode is PracticeMode.BLIND_REPEAT and not scenario_revealed
                else tuple(check.type for check in public_definition.success_checks)
            ),
            hint_count=len(public_definition.hints),
            interview_questions=definition.interview.questions,
            practice_mode=practice_mode,
            scenario_revealed=scenario_revealed,
            scenario_name=(
                active_variant.definition.metadata.name
                if scenario_revealed and active_variant
                else None
            ),
            scenario_description=(
                active_variant.definition.metadata.description
                if scenario_revealed and active_variant
                else None
            ),
            key_evidence=reveal.key_evidence if reveal else None,
            root_cause=reveal.root_cause if reveal else None,
            resolution=reveal.resolution if reveal else None,
            prevention=reveal.prevention if reveal else None,
            fault_map=fault_map,
            learning_path_ids=learning_path_ids,
            knowledge_before=knowledge_before,
            knowledge_after=knowledge_after,
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
        with self._unit_of_work() as uow:
            revealed = session.variant_id == "baseline" or uow.sessions.has_event(
                session.id, "success_contract_passed"
            )
        return SessionStatusResult(
            session=session,
            namespace_exists=None,
            namespace_owned=None,
            cluster_state=ClusterState.NOT_CHECKED,
            stage=_session_stage(session.status),
            practice_mode=_practice_mode(session),
            scenario_revealed=revealed,
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
                title=(
                    "复练场景已选择"
                    if session.variant_id != "baseline" and event.event_type == "session_created"
                    else _event_title(event.event_type)
                ),
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
            lab = self._resolve_session_lab(session)
            if session.status is SessionStatus.READY:
                session = self._transition(
                    session.id,
                    SessionStatus.IN_PROGRESS,
                    event_type="hint_requested",
                )
            with self._unit_of_work() as uow:
                used = uow.hints.used_levels(session.id)
                level = min(len(used) + 1, len(lab.definition.hints))
                newly_unlocked, request_count, unlocked_count = uow.hints.record_request(
                    session.id, level
                )
                uow.commit()
            hint = next(item for item in lab.definition.hints if item.level == level)
            return HintResult(
                session_id=session.id,
                lab_id=session.lab_id,
                level=level,
                total_levels=len(lab.definition.hints),
                content=hint.content,
                newly_unlocked=newly_unlocked,
                kind=_hint_kind(level),
                request_count=request_count,
                unlocked_count=unlocked_count,
            )

    def retrospective(self, session_id: str | None = None) -> RetrospectiveEditState:
        """Load the active or latest Session retrospective for CLI editing."""
        session = self.session_snapshot(session_id, latest_if_inactive=True)
        with self._unit_of_work() as uow:
            retrospective = uow.retrospectives.get(session.id)
        return RetrospectiveEditState(
            session=session,
            retrospective=retrospective,
            metadata=self._retrospective_metadata(session),
        )

    def progress(self) -> LearningProgressReport:
        """Derive learning outcomes only from lifecycle records."""
        registry = self._registry.scan()
        with self._unit_of_work() as uow:
            sessions = uow.sessions.list_all()
            event_map = {session.id: uow.sessions.list_events(session.id) for session in sessions}
        lab_items: list[LabLearningProgress] = []
        category_totals: dict[str, list[int]] = {}
        for loaded in registry.labs:
            metadata = loaded.definition.metadata
            attempts = tuple(session for session in sessions if session.lab_id == metadata.id)
            completion_times = sorted(
                passed_at
                for session in attempts
                if (
                    passed_at := next(
                        (
                            event.created_at
                            for event in event_map[session.id]
                            if event.event_type == "success_contract_passed"
                        ),
                        None,
                    )
                )
                is not None
            )
            completion_count = len(completion_times)
            passed_variants = _passed_variants(metadata.id, sessions, event_map)
            baseline_completed = "baseline" in passed_variants
            completed_variant_ids = passed_variants - {"baseline"}
            revealed_names = tuple(
                variant.definition.metadata.name
                for variant in loaded.variants
                if variant.definition.metadata.id in completed_variant_ids
            )
            variant_attempts = tuple(
                session for session in attempts if session.variant_id != "baseline"
            )
            lab_items.append(
                LabLearningProgress(
                    lab_id=metadata.id,
                    name=metadata.name,
                    category=metadata.category,
                    attempt_count=len(attempts),
                    completion_count=completion_count,
                    repeat_completion_count=max(completion_count - 1, 0),
                    first_completed_at=completion_times[0] if completion_times else None,
                    last_completed_at=completion_times[-1] if completion_times else None,
                    baseline_completed=baseline_completed,
                    variant_total=len(loaded.variants),
                    variant_completed=len(completed_variant_ids),
                    variant_attempt_count=len(variant_attempts),
                    last_practiced_at=(
                        max(session.created_at for session in variant_attempts)
                        if variant_attempts
                        else None
                    ),
                    revealed_scenarios=revealed_names,
                )
            )
            totals = category_totals.setdefault(metadata.category, [0, 0, 0])
            totals[0] += 1
            totals[1] += int(completion_count > 0)
            totals[2] += len(attempts)
        categories = tuple(
            CategoryLearningProgress(
                category=category,
                lab_count=totals[0],
                completed_lab_count=totals[1],
                attempt_count=totals[2],
            )
            for category, totals in sorted(category_totals.items())
        )
        return LearningProgressReport(labs=tuple(lab_items), categories=categories)

    def learning_paths(self) -> LearningPathCatalogReport:
        """Return all M7 paths with progress derived from existing Session facts."""
        catalog, loaded_labs = self._require_learning_catalog()
        facts = self._learning_facts(loaded_labs)
        cards = {card.lab_id: card for card in catalog.knowledge_cards}
        details = tuple(evaluate_path(path, cards, facts) for path in catalog.paths)
        return LearningPathCatalogReport(paths=tuple(detail.summary for detail in details))

    def learning_path(self, path_id: str) -> LearningPathDetail:
        """Return one path map including exact, explainable lock reasons."""
        catalog, loaded_labs = self._require_learning_catalog()
        definition = self._find_learning_path(catalog, path_id)
        cards = {card.lab_id: card for card in catalog.knowledge_cards}
        return evaluate_path(definition, cards, self._learning_facts(loaded_labs))

    def learning_recommendation(self) -> LearningRecommendation:
        """Return one deterministic next action; an active Session always wins."""
        catalog, loaded_labs = self._require_learning_catalog()
        facts = self._learning_facts(loaded_labs)
        cards = {card.lab_id: card for card in catalog.knowledge_cards}
        details = tuple(evaluate_path(path, cards, facts) for path in catalog.paths)
        return recommend_next(catalog.paths, details, facts)

    def symptoms(self) -> SymptomCatalog:
        """Return the static symptom index without inspecting the cluster."""
        catalog, _ = self._require_learning_catalog()
        return SymptomCatalog(symptoms=catalog.symptoms)

    def learning_path_outcome(self, path_id: str) -> LearningPathOutcome:
        """Derive one topic outcome without creating a second progress state."""
        catalog, loaded_labs = self._require_learning_catalog()
        definition = self._find_learning_path(catalog, path_id)
        return derive_outcome(definition, self._learning_facts(loaded_labs))

    def export_learning_path_outcome(self, path_id: str) -> str:
        """Export a bounded, re-redacted Markdown topic outcome."""
        return render_outcome_markdown(self.learning_path_outcome(path_id))

    def export_retrospective(self, session_id: str | None = None) -> str:
        """Render a bounded, re-redacted Markdown learning record."""
        state = self.retrospective(session_id)
        value = state.retrospective or RetrospectiveSnapshot(
            session_id=state.session.id,
            updated_at=state.session.created_at,
        )
        metadata = state.metadata
        assert metadata is not None
        lines = [
            "# KubeLab 脱敏复盘",
            "",
            "## 实验元数据",
            "",
            f"- 实验：{_markdown_text(metadata.lab_name)} (`{metadata.lab_id}`)",
            f"- 分类 / 难度：{metadata.category} / {metadata.difficulty}",
            f"- Session：`{metadata.session_id}`",
            f"- Namespace：`{metadata.namespace}`",
            f"- 开始时间：{_iso_or_dash(metadata.started_at)}",
            f"- 首次通过：{_iso_or_dash(metadata.first_passed_at)}",
            f"- 清理时间：{_iso_or_dash(metadata.completed_at)}",
            f"- 完成耗时：{_duration_text(metadata.completion_duration_seconds)}",
            f"- 提示请求 / 解锁：{metadata.hint_request_count} / {metadata.unlocked_hint_count}",
            f"- 手动验证 / 重置：{metadata.manual_verification_count} / {metadata.reset_count}",
            "",
        ]
        if metadata.scenario_name is not None:
            lines.extend(
                (
                    "## 本次复练场景",
                    "",
                    f"- 场景：{_markdown_text(metadata.scenario_name)}",
                    f"- 说明：{_markdown_text(metadata.scenario_description or '')}",
                    f"- 关键证据：{_markdown_text(metadata.key_evidence or '')}",
                    f"- 根因：{_markdown_text(metadata.scenario_root_cause or '')}",
                    f"- 标准修复：{_markdown_text(metadata.scenario_resolution or '')}",
                    f"- 预防：{_markdown_text(metadata.scenario_prevention or '')}",
                    "",
                )
            )
        sections = (
            ("现象", value.symptom),
            ("影响", value.impact),
            ("调查过程", value.investigation),
            ("根因", value.root_cause),
            ("修复", value.resolution),
            ("预防措施", value.prevention),
            ("面试总结", value.interview_summary),
        )
        for title, content in sections:
            lines.extend((f"## {title}", "", _markdown_text(content) or "（未填写）", ""))
        if metadata.last_verification is not None:
            lines.extend(("## 最后一次公开验证", ""))
            lines.append(f"- 总体状态：{metadata.last_verification.status}")
            for check in metadata.last_verification.results:
                lines.append(
                    f"- `{check.check_id}`：{check.status} — {_markdown_text(check.message)}"
                )
            lines.append("")
        return "\n".join(lines)[:50_000]

    def _retrospective_metadata(self, session: LabSessionSnapshot) -> RetrospectiveMetadata:
        lab = self._require_lab(session.lab_id)
        with self._unit_of_work() as uow:
            events = uow.sessions.list_events(session.id)
            hints = uow.hints.list_for_session(session.id)
            verifications = uow.verifications.list_for_session(session.id)
            latest = uow.verifications.latest_for_session(session.id)
        passed_at = next(
            (event.created_at for event in events if event.event_type == "success_contract_passed"),
            None,
        )
        variant = None
        if session.variant_id != "baseline" and passed_at is not None:
            effective = self._resolve_session_lab(session, loaded=lab)
            assert isinstance(effective, EffectiveLab)
            variant = effective.variant
        started_at = session.started_at
        duration = (
            max(int((passed_at - started_at).total_seconds()), 0)
            if passed_at is not None and started_at is not None
            else None
        )
        last_verification = None
        if latest is not None:
            last_verification = PublicVerificationSummary(
                status=public_validation_outcome(latest.status).value,
                checked_at=latest.checked_at,
                duration_ms=latest.duration_ms,
                results=tuple(
                    PublicVerificationCheckSummary(
                        check_id=item.check_id,
                        check_type=item.check_type,
                        status=public_validation_outcome(item.status).value,
                        message=str(redact_json(item.message))[:500],
                        retryable=item.retryable,
                        duration_ms=item.duration_ms,
                    )
                    for item in latest.results
                ),
            )
        metadata = lab.definition.metadata
        return RetrospectiveMetadata(
            lab_id=metadata.id,
            lab_name=metadata.name,
            category=metadata.category,
            difficulty=metadata.difficulty,
            session_id=session.id,
            namespace=session.namespace,
            started_at=started_at,
            first_passed_at=passed_at,
            completed_at=session.completed_at,
            hint_request_count=sum(item.request_count for item in hints),
            unlocked_hint_count=len(hints),
            manual_verification_count=sum(
                item.purpose is VerificationPurpose.MANUAL for item in verifications
            ),
            reset_count=session.reset_count,
            completion_duration_seconds=duration,
            last_verification=last_verification,
            practice_mode=_practice_mode(session),
            scenario_name=variant.definition.metadata.name if variant else None,
            scenario_description=variant.definition.metadata.description if variant else None,
            key_evidence=variant.definition.reveal.key_evidence if variant else None,
            scenario_root_cause=variant.definition.reveal.root_cause if variant else None,
            scenario_resolution=variant.definition.reveal.resolution if variant else None,
            scenario_prevention=variant.definition.reveal.prevention if variant else None,
        )

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
            parent_lab = self._require_lab(lab_id)
            if self._readiness is not None:
                self._readiness.assert_ready(parent_lab.definition.requirements)
            trusted = self._context_trust.assert_trusted_context()
            fingerprint = trusted_context_fingerprint(trusted)
            with self._unit_of_work() as uow:
                active = uow.sessions.get_active()
                if active is not None:
                    raise ActiveSessionConflict(active)
                variant_id = self._select_variant(parent_lab, uow)
                session = uow.sessions.create(
                    NewLabSession(
                        id=str(uuid4()),
                        lab_id=lab_id,
                        variant_id=variant_id,
                        namespace=parent_lab.definition.environment.namespace,
                        context_name=trusted.name,
                        context_fingerprint=fingerprint,
                    )
                )
                uow.commit()

            lab = self._resolve_session_lab(session, loaded=parent_lab)
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
                    practice_mode=_practice_mode(session),
                    scenario_revealed=self._scenario_revealed(session),
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
                        practice_mode=_practice_mode(completed),
                        scenario_revealed=self._scenario_revealed(completed),
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
                    practice_mode=_practice_mode(session),
                    scenario_revealed=self._scenario_revealed(session),
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
            lab = self._resolve_session_lab(session)
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
            lab = self._resolve_session_lab(session)
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
                    with self._unit_of_work() as uow:
                        uow.sessions.transition(
                            session.id,
                            SessionStatus.PASSED,
                            event_type="success_contract_passed",
                            context={"verification_run_id": result.id},
                        )
                        if session.variant_id != "baseline":
                            uow.sessions.record_event(session.id, "scenario_revealed")
                        uow.commit()
                return result
            finally:
                gateway.close()

    def _select_variant(self, loaded: LoadedLab, uow: SqlAlchemyUnitOfWork) -> str:
        variants = loaded.variants
        if not variants:
            return "baseline"
        sessions = uow.sessions.list_for_lab(loaded.definition.metadata.id)
        event_map = {session.id: uow.sessions.list_events(session.id) for session in sessions}
        passed = _passed_variants(loaded.definition.metadata.id, sessions, event_map)
        if "baseline" not in passed:
            return "baseline"

        nonbaseline = tuple(session for session in sessions if session.variant_id != "baseline")
        if nonbaseline:
            latest = max(nonbaseline, key=lambda item: (item.created_at, item.id))
            if not _has_passed(event_map[latest.id]):
                return latest.variant_id

        for variant in variants:
            variant_id = variant.definition.metadata.id
            if variant_id not in passed:
                return variant_id

        last_practiced = {
            variant.definition.metadata.id: max(
                (
                    session.created_at
                    for session in nonbaseline
                    if session.variant_id == variant.definition.metadata.id
                ),
                default=datetime.min.replace(tzinfo=UTC),
            )
            for variant in variants
        }
        # All variants have at least one successful Session here; the fallback above is defensive.
        return min(
            variants,
            key=lambda variant: (
                last_practiced[variant.definition.metadata.id],
                variant.definition.metadata.sequence,
            ),
        ).definition.metadata.id

    def _resolve_session_lab(
        self, session: LabSessionSnapshot, *, loaded: LoadedLab | None = None
    ) -> ExecutableLab:
        parent = loaded or self._require_lab(session.lab_id)
        try:
            return self._registry.resolve_variant(parent, session.variant_id)
        except LabVariantNotFoundError as exc:
            raise LabManagerError(
                ManagerErrorCode.LAB_VARIANT_NOT_FOUND,
                "The Session's fixed lab variant is no longer available.",
            ) from exc
        except LabMaterializationError as exc:
            raise LabManagerError(
                ManagerErrorCode.LAB_INVALID,
                "The Session's fixed lab variant changed after validation.",
                retryable=True,
            ) from exc

    def _scenario_revealed(self, session: LabSessionSnapshot) -> bool:
        if session.variant_id == "baseline":
            return True
        with self._unit_of_work() as uow:
            return uow.sessions.has_event(session.id, "success_contract_passed")

    @staticmethod
    def _catalog_item(
        loaded: LoadedLab,
        *,
        active: LabSessionSnapshot | None,
        passed: frozenset[str],
        sessions: tuple[LabSessionSnapshot, ...],
        event_map: dict[str, tuple[SessionEventSnapshot, ...]],
    ) -> LabCatalogItem:
        metadata = loaded.definition.metadata
        if active is not None and active.lab_id == metadata.id:
            progress = LabProgress.ACTIVE
        elif metadata.id in passed:
            progress = LabProgress.COMPLETED
        else:
            progress = LabProgress.NOT_STARTED
        passed_variants = _passed_variants(metadata.id, sessions, event_map)
        return LabCatalogItem(
            id=metadata.id,
            name=metadata.name,
            description=metadata.description,
            difficulty=metadata.difficulty,
            duration_minutes=metadata.duration_minutes,
            category=metadata.category,
            tags=metadata.tags,
            progress=progress,
            baseline_completed="baseline" in passed_variants,
            variant_total=len(loaded.variants),
            variant_completed=len(passed_variants - {"baseline"}),
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

    def _optional_learning_catalog(self) -> LearningPathCatalogDefinition | None:
        if self._learning_path_registry is None:
            return None
        labs = self._registry.scan().labs
        snapshot = self._learning_path_registry.scan(
            frozenset(lab.definition.metadata.id for lab in labs)
        )
        return snapshot.catalog

    def _require_learning_catalog(
        self,
    ) -> tuple[LearningPathCatalogDefinition, tuple[LoadedLab, ...]]:
        labs = self._registry.scan().labs
        if self._learning_path_registry is None:
            raise LabManagerError(
                ManagerErrorCode.LEARNING_PATH_INVALID,
                "The learning path catalog is unavailable.",
            )
        snapshot = self._learning_path_registry.scan(
            frozenset(lab.definition.metadata.id for lab in labs)
        )
        if snapshot.catalog is None:
            raise LabManagerError(
                ManagerErrorCode.LEARNING_PATH_INVALID,
                "The learning path catalog is invalid.",
                context={"error_count": len(snapshot.errors)},
            )
        return snapshot.catalog, labs

    @staticmethod
    def _find_learning_path(
        catalog: LearningPathCatalogDefinition, path_id: str
    ) -> LearningPathDefinition:
        match = next((path for path in catalog.paths if path.metadata.id == path_id), None)
        if match is None:
            raise LabManagerError(
                ManagerErrorCode.LEARNING_PATH_NOT_FOUND,
                "The requested learning path was not found.",
            )
        return match

    def _learning_facts(self, loaded_labs: tuple[LoadedLab, ...]) -> LearningFacts:
        with self._unit_of_work() as uow:
            sessions = uow.sessions.list_all()
            active = uow.sessions.get_active()
            event_map = {session.id: uow.sessions.list_events(session.id) for session in sessions}
            hint_map = {session.id: uow.hints.list_for_session(session.id) for session in sessions}
            verification_map = {
                session.id: uow.verifications.list_for_session(session.id) for session in sessions
            }

        lab_facts: dict[str, LabLearningFacts] = {}
        for loaded in loaded_labs:
            lab_id = loaded.definition.metadata.id
            attempts = tuple(session for session in sessions if session.lab_id == lab_id)
            ordered_attempts = sorted(attempts, key=lambda item: (item.created_at, item.id))
            latest = ordered_attempts[-1] if ordered_attempts else None
            completion_times = sorted(
                event.created_at
                for session in attempts
                for event in event_map[session.id]
                if event.event_type == "success_contract_passed"
            )
            passed_variants = _passed_variants(lab_id, sessions, event_map)
            completed_variant_ids = passed_variants - {"baseline"}
            latest_hints = hint_map.get(latest.id, ()) if latest is not None else ()
            latest_verifications = verification_map.get(latest.id, ()) if latest is not None else ()
            lab_facts[lab_id] = LabLearningFacts(
                lab_id=lab_id,
                baseline_completed="baseline" in passed_variants,
                variant_total=len(loaded.variants),
                variant_completed=len(completed_variant_ids),
                attempt_count=len(attempts),
                completion_count=len(completion_times),
                latest_attempt_passed=(
                    _has_passed(event_map[latest.id]) if latest is not None else None
                ),
                latest_unlocked_hint_count=len(latest_hints),
                latest_manual_verification_count=sum(
                    run.purpose is VerificationPurpose.MANUAL for run in latest_verifications
                ),
                hint_request_count=sum(
                    hint.request_count for session in attempts for hint in hint_map[session.id]
                ),
                manual_verification_count=sum(
                    run.purpose is VerificationPurpose.MANUAL
                    for session in attempts
                    for run in verification_map[session.id]
                ),
                first_completed_at=completion_times[0] if completion_times else None,
                last_completed_at=completion_times[-1] if completion_times else None,
                last_practiced_at=(
                    max(session.created_at for session in attempts) if attempts else None
                ),
                revealed_scenarios=tuple(
                    variant.definition.metadata.name
                    for variant in loaded.variants
                    if variant.definition.metadata.id in completed_variant_ids
                ),
            )
        return LearningFacts(
            labs=lab_facts,
            active_lab_id=active.lab_id if active is not None else None,
            active_variant_id=active.variant_id if active is not None else None,
        )

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
        "scenario_revealed": "复练故障场景已揭示",
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


def _hint_kind(level: int) -> HintKind:
    return {
        1: HintKind.OBSERVATION,
        2: HintKind.COMMAND,
        3: HintKind.FAULT_DIRECTION,
    }[level]


def _practice_mode(session: LabSessionSnapshot) -> PracticeMode:
    return PracticeMode.BASELINE if session.variant_id == "baseline" else PracticeMode.BLIND_REPEAT


def _has_passed(events: tuple[SessionEventSnapshot, ...]) -> bool:
    return any(event.event_type == "success_contract_passed" for event in events)


def _passed_variants(
    lab_id: str,
    sessions: tuple[LabSessionSnapshot, ...],
    event_map: dict[str, tuple[SessionEventSnapshot, ...]],
) -> frozenset[str]:
    return frozenset(
        session.variant_id
        for session in sessions
        if session.lab_id == lab_id and _has_passed(event_map.get(session.id, ()))
    )


def _fault_map_entry(*, slot: int, variant: LoadedVariant, revealed: bool) -> FaultMapEntry:
    if not revealed:
        return FaultMapEntry(slot=slot, revealed=False)
    metadata = variant.definition.metadata
    reveal = variant.definition.reveal
    return FaultMapEntry(
        slot=slot,
        revealed=True,
        name=metadata.name,
        description=metadata.description,
        key_evidence=reveal.key_evidence,
        root_cause=reveal.root_cause,
        resolution=reveal.resolution,
        prevention=reveal.prevention,
    )


def _markdown_text(value: str) -> str:
    redacted = str(redact_json(value))[:4000]
    escaped = html.escape(redacted, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("\r", "").strip()


def _iso_or_dash(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "—"


def _duration_text(seconds: int | None) -> str:
    return f"{seconds} 秒" if seconds is not None else "—"


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
    "HintKind",
    "InitialContractResult",
    "LabCatalogItem",
    "LabCatalogResult",
    "LabDetailResult",
    "LabManager",
    "LabManagerError",
    "LabLearningProgress",
    "LearningTimelineEntry",
    "LearningProgressReport",
    "LabProgress",
    "ManagerErrorCode",
    "RetrospectiveEditState",
    "RetrospectiveMetadata",
    "SessionEvents",
    "SessionResources",
    "SessionStage",
    "SessionStatusResult",
    "SessionTimeline",
    "ValidationService",
    "WorkspaceAccess",
]
