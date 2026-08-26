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
    GatewayErrorCode,
    KubernetesGatewayError,
    NamespaceDeleteResult,
    SessionScope,
)
from kubelab.lab_manager import (
    InitialContractResult,
    LabManager,
    LabManagerError,
    ManagerErrorCode,
)
from kubelab.lab_registry import LabRegistry, LoadedLab
from kubelab.operation_lock import OperationLock, OperationLockError
from kubelab.repositories import ActiveSessionConflict
from kubelab.session_state import LabSessionSnapshot, SessionStatus, ValidationStatus


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

    def validate_initial_contract(
        self, scope: SessionScope, lab: LoadedLab, gateway: Any
    ) -> InitialContractResult:
        del scope, lab, gateway
        self.calls += 1
        return self.result


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
