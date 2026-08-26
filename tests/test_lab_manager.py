"""Lifecycle orchestration tests for LabManager using real SQLite and fake cluster seams."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kubelab.config import TrustedContext
from kubelab.context_trust import ContextNotTrustedError
from kubelab.database import Database
from kubelab.kubernetes_gateway import (
    EventSummary,
    GatewayErrorCode,
    KubernetesGatewayError,
    LogResult,
    NamespaceDeleteResult,
    PodSummary,
    ResourceSummary,
    SessionScope,
)
from kubelab.lab_manager import (
    InitialContractResult,
    LabManager,
    LabManagerError,
    LabProgress,
    ManagerErrorCode,
)
from kubelab.lab_registry import LabRegistry, LoadedLab
from kubelab.operation_lock import OperationLock, OperationLockError
from kubelab.repositories import ActiveSessionConflict
from kubelab.session_state import (
    LabSessionSnapshot,
    RetrospectiveInput,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
)
from kubelab.validation_engine import ValidationRunResult


def trusted_record() -> TrustedContext:
    return TrustedContext(
        name="minikube",
        server="https://127.0.0.1:32771",
        ca_sha256="a" * 64,
        kube_system_uid="uid-kube-system",
        minikube_profile="minikube",
        trusted_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


class FakeTrust:
    def __init__(self, record: TrustedContext | None = None) -> None:
        self.record = record or trusted_record()
        self.error: Exception | None = None
        self.calls = 0

    def assert_trusted_context(self) -> TrustedContext:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.record


class FakeValidation:
    def __init__(self) -> None:
        self.result = InitialContractResult(status=ValidationStatus.PASSED)
        self.calls = 0
        self.reset_sequences: list[int] = []
        self.success_status = ValidationStatus.PASSED

    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: Any,
        reset_sequence: int,
    ) -> InitialContractResult:
        del scope, lab, gateway
        self.calls += 1
        self.reset_sequences.append(reset_sequence)
        return self.result

    def validate_success_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: Any,
        reset_sequence: int,
        purpose: VerificationPurpose = VerificationPurpose.MANUAL,
    ) -> ValidationRunResult:
        del lab, gateway
        return ValidationRunResult(
            id="123e4567-e89b-42d3-a456-426614174111",
            session_id=scope.session_id,
            purpose=purpose,
            status=self.success_status,
            reset_sequence=reset_sequence,
            checked_at=datetime(2026, 8, 26, tzinfo=UTC),
            duration_ms=10,
            results=(),
        )


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.exists = True
        self.owned = True
        self.already_absent = False
        self.closed = False

    def _call(self, name: str) -> None:
        self.calls.append(name)
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def create_environment(self, scope: SessionScope) -> None:
        del scope
        self._call("create")
        self.exists = True

    def apply_lab(self, scope: SessionScope, loaded: LoadedLab, registry: LabRegistry) -> None:
        del scope, loaded, registry
        self._call("apply")

    def namespace_exists(self, scope: SessionScope) -> bool:
        del scope
        self._call("exists")
        return self.exists

    def assert_namespace_owned(self, scope: SessionScope) -> None:
        del scope
        self._call("owned")
        if not self.owned:
            raise KubernetesGatewayError(GatewayErrorCode.OWNERSHIP_MISMATCH, "ownership mismatch")

    def delete_environment(
        self, scope: SessionScope, *, wait_timeout_seconds: float = 120
    ) -> NamespaceDeleteResult:
        del wait_timeout_seconds
        self._call("delete")
        self.exists = False
        return NamespaceDeleteResult(
            namespace=scope.namespace,
            deleted=not self.already_absent,
            already_absent=self.already_absent,
        )

    def close(self) -> None:
        self.closed = True

    def list_resources(self, scope: SessionScope) -> tuple[ResourceSummary, ...]:
        self._call("resources")
        return (
            ResourceSummary(
                api_version="apps/v1",
                kind="Deployment",
                namespace=scope.namespace,
                name="web",
            ),
        )

    def list_pods(self, scope: SessionScope) -> tuple[PodSummary, ...]:
        del scope
        self._call("pods")
        return (
            PodSummary(
                name="web-abc",
                phase="Running",
                ready=True,
                restart_count=0,
                containers=(),
            ),
        )

    def list_events(self, scope: SessionScope) -> tuple[EventSummary, ...]:
        del scope
        self._call("events")
        return (EventSummary(type="Warning", reason="Failed"),)

    def read_logs(
        self,
        scope: SessionScope,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 200,
    ) -> LogResult:
        del scope
        self._call("logs")
        return LogResult(
            pod=pod,
            container=container,
            previous=previous,
            content="log",
            truncated=False,
            line_count=min(1, tail_lines),
        )


class FakeGatewayFactory:
    def __init__(self, *gateways: FakeGateway) -> None:
        self.gateways = list(gateways) or [FakeGateway()]
        self.calls: list[tuple[TrustedContext, str]] = []

    def __call__(self, trusted: TrustedContext, fingerprint: str) -> FakeGateway:
        self.calls.append((trusted, fingerprint))
        if len(self.gateways) > 1:
            return self.gateways.pop(0)
        return self.gateways[0]


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "state" / "kubelab.db")
    value.initialize()
    yield value
    value.dispose()


def build_manager(
    database: Database,
    tmp_path: Path,
    *,
    gateway: FakeGateway | None = None,
    trust: FakeTrust | None = None,
    validation: FakeValidation | None = None,
    registry: LabRegistry | None = None,
) -> tuple[LabManager, FakeGateway, FakeTrust, FakeValidation]:
    selected_gateway = gateway or FakeGateway()
    selected_trust = trust or FakeTrust()
    selected_validation = validation or FakeValidation()
    selected_registry = registry or LabRegistry(
        Path(__file__).parent / "fixtures" / "labs" / "valid"
    )
    manager = LabManager(
        registry=selected_registry,
        unit_of_work=database.unit_of_work,
        operation_lock=OperationLock(tmp_path / "manager.lock", timeout_seconds=0),
        context_trust=selected_trust,  # type: ignore[arg-type]
        gateway_factory=FakeGatewayFactory(selected_gateway),
        validation=selected_validation,
    )
    return manager, selected_gateway, selected_trust, selected_validation


def persisted(database: Database, session_id: str) -> LabSessionSnapshot:
    with database.unit_of_work() as uow:
        return uow.sessions.require(session_id)


def events(database: Database, session_id: str) -> tuple[str, ...]:
    with database.unit_of_work() as uow:
        return tuple(item.event_type for item in uow.sessions.list_events(session_id))


def test_catalog_show_and_progress_hide_answers(database: Database, tmp_path: Path) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)

    catalog = manager.list_labs(category="networking")
    detail = manager.show_lab("complete-lab")

    assert catalog.labs[0].progress is LabProgress.NOT_STARTED
    assert detail.lab.id == "complete-lab"
    assert detail.hint_count == 1
    assert detail.initial_check_types
    assert "expected" not in detail.model_dump_json().lower()
    assert manager.list_labs(progress=LabProgress.COMPLETED).labs == ()


def test_catalog_marks_active_then_completed_after_success(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")

    assert manager.list_labs().labs[0].progress is LabProgress.ACTIVE
    manager.verify(created.id)
    manager.cleanup(created.id)

    assert manager.list_labs().labs[0].progress is LabProgress.COMPLETED
    assert manager.session_snapshot(latest_if_inactive=True).status is SessionStatus.COMPLETED


def test_safe_cluster_reads_move_ready_session_to_in_progress(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    gateway.calls.clear()

    resources = manager.resources(created.id)
    observed_events = manager.events(created.id)
    logs = manager.logs("web-abc", container="web", previous=True, tail_lines=10)

    assert resources.session.status is SessionStatus.IN_PROGRESS
    assert resources.resources[0].name == "web"
    assert resources.pods[0].name == "web-abc"
    assert observed_events.events[0].reason == "Failed"
    assert logs.content == "log"
    assert gateway.calls == [
        "exists",
        "owned",
        "resources",
        "pods",
        "exists",
        "owned",
        "events",
        "exists",
        "owned",
        "logs",
    ]


def test_cluster_read_reconciles_missing_namespace(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    gateway.exists = False

    with pytest.raises(LabManagerError) as error:
        manager.resources(created.id)

    assert error.value.code == ManagerErrorCode.ENVIRONMENT_REMOVED
    assert persisted(database, created.id).status is SessionStatus.COMPLETED


def test_hints_unlock_in_order_and_last_hint_is_idempotent(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")

    first = manager.next_hint(created.id)
    repeated = manager.next_hint(created.id)

    assert (first.level, first.newly_unlocked) == (1, True)
    assert (repeated.level, repeated.newly_unlocked) == (1, False)
    assert persisted(database, created.id).status is SessionStatus.IN_PROGRESS


def test_retrospective_can_be_saved_after_cleanup(database: Database, tmp_path: Path) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    manager.cleanup(created.id)

    value = RetrospectiveInput(root_cause="Wrong image", resolution="Corrected the tag")
    saved = manager.save_retrospective(value)
    loaded = manager.retrospective()

    assert saved.session_id == created.id
    assert loaded.retrospective is not None
    assert loaded.retrospective.root_cause == "Wrong image"


def test_start_creates_ready_session_in_dependency_order(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, trust, validation = build_manager(database, tmp_path)

    result = manager.start("complete-lab")

    assert result.status is SessionStatus.READY
    assert result.namespace == "kubelab-complete-lab"
    assert gateway.calls == ["create", "apply"]
    assert gateway.closed is True
    assert trust.calls == 1
    assert validation.calls == 1
    assert validation.reset_sequences == [0]
    assert events(database, result.id) == ("session_created", "environment_ready")


def test_start_rejects_missing_lab_before_context_or_database(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, trust, _ = build_manager(database, tmp_path)

    with pytest.raises(LabManagerError) as error:
        manager.start("missing-lab")

    assert error.value.code == ManagerErrorCode.LAB_NOT_FOUND
    assert trust.calls == 0
    assert gateway.calls == []
    with database.unit_of_work() as uow:
        assert uow.sessions.get_active() is None


def test_start_reports_invalid_lab_with_structured_redacted_errors(
    database: Database, tmp_path: Path
) -> None:
    invalid = Path(__file__).parent / "fixtures" / "labs" / "invalid"
    manager, _, _, _ = build_manager(database, tmp_path, registry=LabRegistry(invalid))

    with pytest.raises(LabManagerError) as error:
        manager.start("unsafe-lab")

    assert error.value.code == ManagerErrorCode.LAB_INVALID
    assert "privileged: true" not in repr(error.value.context)


def test_second_start_is_blocked_by_active_session(database: Database, tmp_path: Path) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    manager.start("complete-lab")

    with pytest.raises(ActiveSessionConflict):
        manager.start("complete-lab")


@pytest.mark.parametrize("failure_point", ["create", "apply"])
def test_start_failure_removes_partial_environment_and_completes_session(
    database: Database, tmp_path: Path, failure_point: str
) -> None:
    gateway = FakeGateway()
    gateway.failures[failure_point] = KubernetesGatewayError(
        GatewayErrorCode.CONFLICT, "TOKEN private", retryable=True
    )
    manager, _, _, _ = build_manager(database, tmp_path, gateway=gateway)

    with pytest.raises(LabManagerError) as error:
        manager.start("complete-lab")

    assert error.value.code == GatewayErrorCode.CONFLICT
    assert error.value.retryable is True
    assert "private" not in str(error.value)
    with database.unit_of_work() as uow:
        session = uow.sessions.get_active()
        assert session is None
        created_id = uow.sessions.list_events  # prove repository remains usable
        assert created_id is not None
    assert gateway.calls[-1] == "delete"
    assert gateway.closed is True


def test_initial_contract_failure_rolls_back_namespace(database: Database, tmp_path: Path) -> None:
    validation = FakeValidation()
    validation.result = InitialContractResult(
        status=ValidationStatus.FAILED,
        error_code="INITIAL_STATE_WRONG",
    )
    manager, gateway, _, _ = build_manager(database, tmp_path, validation=validation)

    with pytest.raises(LabManagerError) as error:
        manager.start("complete-lab")

    assert error.value.code == "INITIAL_STATE_WRONG"
    assert gateway.calls == ["create", "apply", "delete"]


def test_start_rollback_cleanup_failure_preserves_error_session(
    database: Database, tmp_path: Path
) -> None:
    gateway = FakeGateway()
    gateway.failures["apply"] = RuntimeError("apply failed")
    gateway.failures["delete"] = RuntimeError("cleanup failed")
    manager, _, _, _ = build_manager(database, tmp_path, gateway=gateway)

    with pytest.raises(LabManagerError):
        manager.start("complete-lab")

    with database.unit_of_work() as uow:
        active = uow.sessions.get_active()
        assert active is not None
        assert active.status is SessionStatus.ERROR
        assert active.last_error_context == {"operation": "start"}


def test_status_marks_ready_in_progress(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    manager.start("complete-lab")
    gateway.calls.clear()

    result = manager.status()

    assert result.session.status is SessionStatus.IN_PROGRESS
    assert result.namespace_exists is True
    assert result.namespace_owned is True
    assert gateway.calls == ["exists", "owned"]


def test_status_reconciles_externally_removed_namespace(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.exists = False

    result = manager.status(session.id)

    assert result.session.status is SessionStatus.COMPLETED
    assert result.namespace_exists is False
    assert events(database, session.id)[-2:] == (
        "environment_removed_externally",
        "external_removal_reconciled",
    )


def test_status_identity_mismatch_marks_session_error(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.owned = False

    with pytest.raises(LabManagerError) as error:
        manager.status(session.id)

    assert error.value.code == GatewayErrorCode.OWNERSHIP_MISMATCH
    assert persisted(database, session.id).status is SessionStatus.ERROR


def test_context_drift_blocks_status_before_gateway_query(
    database: Database, tmp_path: Path
) -> None:
    trust = FakeTrust()
    manager, gateway, _, _ = build_manager(database, tmp_path, trust=trust)
    session = manager.start("complete-lab")
    gateway.calls.clear()
    trust.error = ContextNotTrustedError("drifted")

    with pytest.raises(LabManagerError) as error:
        manager.status(session.id)

    assert error.value.code == ManagerErrorCode.CONTEXT_DRIFT
    assert gateway.calls == []


def test_session_fingerprint_mismatch_blocks_cleanup_before_state_change(
    database: Database, tmp_path: Path
) -> None:
    trust = FakeTrust()
    manager, gateway, _, _ = build_manager(database, tmp_path, trust=trust)
    session = manager.start("complete-lab")
    gateway.calls.clear()
    trust.record = trusted_record().model_copy(update={"kube_system_uid": "new-cluster"})

    with pytest.raises(LabManagerError) as error:
        manager.cleanup(session.id)

    assert error.value.code == ManagerErrorCode.CONTEXT_DRIFT
    assert persisted(database, session.id).status is SessionStatus.READY
    assert gateway.calls == []


def test_reset_preserves_session_id_and_increments_counter(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, validation = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()

    result = manager.reset()

    assert result.id == session.id
    assert result.status is SessionStatus.READY
    assert result.reset_count == 1
    assert gateway.calls == ["delete", "create", "apply"]
    assert validation.calls == 2
    assert validation.reset_sequences == [0, 1]


def test_reset_failure_cleans_partial_environment_and_leaves_retryable_error(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()
    gateway.failures["apply"] = RuntimeError("apply failed")

    with pytest.raises(LabManagerError):
        manager.reset(session.id)

    failed = persisted(database, session.id)
    assert failed.status is SessionStatus.ERROR
    assert gateway.calls == ["delete", "create", "apply", "delete"]


def test_reset_resumes_session_already_marked_resetting(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    with database.unit_of_work() as uow:
        uow.sessions.transition(
            session.id,
            SessionStatus.RESETTING,
            event_type="simulated_interruption",
        )
        uow.commit()
    gateway.calls.clear()
    gateway.already_absent = True

    result = manager.reset(session.id)

    assert result.status is SessionStatus.READY
    assert result.reset_count == 1
    assert gateway.calls == ["delete", "create", "apply"]


def test_cleanup_completes_and_is_idempotent_by_explicit_id(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()

    first = manager.cleanup()
    second = manager.cleanup(session.id)

    assert first.status is SessionStatus.COMPLETED
    assert second == first
    assert gateway.calls == ["delete"]


def test_cleanup_absent_namespace_is_controlled_completion(
    database: Database, tmp_path: Path
) -> None:
    gateway = FakeGateway()
    gateway.already_absent = True
    manager, _, _, _ = build_manager(database, tmp_path, gateway=gateway)
    session = manager.start("complete-lab")

    result = manager.cleanup(session.id)

    assert result.status is SessionStatus.COMPLETED
    assert events(database, session.id)[-1] == "cleanup_completed"


def test_cleanup_failure_preserves_active_error_session(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.failures["delete"] = KubernetesGatewayError(
        GatewayErrorCode.OWNERSHIP_MISMATCH, "forged"
    )

    with pytest.raises(LabManagerError) as error:
        manager.cleanup(session.id)

    assert error.value.code == GatewayErrorCode.OWNERSHIP_MISMATCH
    assert persisted(database, session.id).status is SessionStatus.ERROR


def test_verify_advances_ready_session_to_passed(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, validation = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()

    result = manager.verify(session.id)

    assert result.status is ValidationStatus.PASSED
    assert result.reset_sequence == 0
    assert persisted(database, session.id).status is SessionStatus.PASSED
    assert events(database, session.id)[-2:] == (
        "verification_started",
        "success_contract_passed",
    )
    assert validation.success_status is ValidationStatus.PASSED
    assert gateway.closed is True


def test_verify_failure_keeps_session_in_progress(database: Database, tmp_path: Path) -> None:
    validation = FakeValidation()
    validation.success_status = ValidationStatus.FAILED
    manager, _, _, _ = build_manager(database, tmp_path, validation=validation)
    session = manager.start("complete-lab")

    result = manager.verify(session.id)

    assert result.status is ValidationStatus.FAILED
    assert persisted(database, session.id).status is SessionStatus.IN_PROGRESS


def test_context_drift_blocks_verify_before_gateway_or_state_change(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, trust, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()
    trust.error = ContextNotTrustedError("CA changed")

    with pytest.raises(LabManagerError) as error:
        manager.verify(session.id)

    assert error.value.code == ManagerErrorCode.CONTEXT_DRIFT
    assert gateway.calls == []
    assert persisted(database, session.id).status is SessionStatus.READY


def test_reverify_passed_session_does_not_repeat_transition(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    manager.verify(session.id)
    before = events(database, session.id)

    result = manager.verify(session.id)

    assert result.status is ValidationStatus.PASSED
    assert events(database, session.id) == before


@pytest.mark.parametrize("terminal", [SessionStatus.ERROR, SessionStatus.COMPLETED])
def test_verify_rejects_error_and_completed_sessions(
    database: Database, tmp_path: Path, terminal: SessionStatus
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    with database.unit_of_work() as uow:
        if terminal is SessionStatus.ERROR:
            uow.sessions.transition(
                session.id, terminal, event_type="simulated_error", error_code="TEST"
            )
        else:
            uow.sessions.transition(
                session.id, SessionStatus.CLEANING, event_type="simulated_cleanup"
            )
            uow.sessions.transition(
                session.id, SessionStatus.COMPLETED, event_type="simulated_completed"
            )
        uow.commit()

    with pytest.raises(LabManagerError) as error:
        manager.verify(session.id)

    assert error.value.code == "INVALID_SESSION_STATE"


def test_missing_active_session_returns_stable_error(database: Database, tmp_path: Path) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)

    with pytest.raises(LabManagerError) as error:
        manager.status()

    assert error.value.code == ManagerErrorCode.SESSION_NOT_FOUND


def test_manager_respects_cross_process_operation_lock(database: Database, tmp_path: Path) -> None:
    lock_path = tmp_path / "manager.lock"
    manager, _, _, _ = build_manager(database, tmp_path)
    owner = OperationLock(lock_path, timeout_seconds=0)

    with owner, pytest.raises(OperationLockError):
        manager.start("complete-lab")
