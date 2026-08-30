"""Lifecycle orchestration tests for LabManager using real SQLite and fake cluster seams."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kubelab.config import TrustedContext
from kubelab.context_trust import ContextNotTrustedError
from kubelab.database import Database
from kubelab.guided_learning import (
    EnvironmentNotReadyError,
    EnvironmentReadinessReport,
    ReadinessCheck,
    ReadinessCheckStatus,
    ReadinessStatus,
)
from kubelab.kubernetes_gateway import (
    EventSummary,
    GatewayErrorCode,
    KubernetesGatewayError,
    LogResult,
    NamespaceDeleteResult,
    PodSummary,
    ResourceSummary,
    SessionScope,
    WorkspaceAccess,
)
from kubelab.lab_manager import (
    ClusterState,
    HintKind,
    InitialContractResult,
    LabManager,
    LabManagerError,
    LabProgress,
    ManagerErrorCode,
    SessionStage,
)
from kubelab.lab_registry import LabRegistry, LoadedLab
from kubelab.operation_lock import OperationLock, OperationLockError
from kubelab.repositories import ActiveSessionConflict
from kubelab.session_state import (
    CheckResultInput,
    LabSessionSnapshot,
    RetrospectiveInput,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
    VerificationRunInput,
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


class FakeReadiness:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls = 0

    def assert_ready(self, requirements: Any) -> EnvironmentReadinessReport:
        del requirements
        self.calls += 1
        if self.error is not None:
            raise self.error
        return EnvironmentReadinessReport(
            status=ReadinessStatus.READY,
            checks=(),
            generated_at=datetime(2026, 8, 26, tzinfo=UTC),
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

    def provision_workspace(self, scope: SessionScope) -> WorkspaceAccess:
        self._call("workspace")
        return WorkspaceAccess(
            session_id=scope.session_id,
            namespace=scope.namespace,
            token="temporary-token",
        )

    def revoke_workspace(self, scope: SessionScope) -> None:
        del scope
        self._call("workspace-revoke")

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
    readiness: FakeReadiness | None = None,
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
        readiness=readiness,
    )
    return manager, selected_gateway, selected_trust, selected_validation


def persisted(database: Database, session_id: str) -> LabSessionSnapshot:
    with database.unit_of_work() as uow:
        return uow.sessions.require(session_id)


def events(database: Database, session_id: str) -> tuple[str, ...]:
    with database.unit_of_work() as uow:
        return tuple(item.event_type for item in uow.sessions.list_events(session_id))


def test_start_readiness_gate_precedes_trust_database_and_cluster_writes(
    database: Database, tmp_path: Path
) -> None:
    readiness = FakeReadiness()
    readiness.error = EnvironmentNotReadyError(
        EnvironmentReadinessReport(
            status=ReadinessStatus.BLOCKED,
            checks=(
                ReadinessCheck(
                    id="docker_daemon",
                    status=ReadinessCheckStatus.FAIL,
                    message="Docker daemon is unavailable.",
                ),
            ),
            generated_at=datetime(2026, 8, 26, tzinfo=UTC),
        )
    )
    manager, gateway, trust, validation = build_manager(database, tmp_path, readiness=readiness)

    with pytest.raises(EnvironmentNotReadyError):
        manager.start("complete-lab")

    assert readiness.calls == 1
    assert trust.calls == 0
    assert gateway.calls == []
    assert validation.calls == 0
    with database.unit_of_work() as uow:
        assert uow.sessions.get_active() is None


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


def test_safe_cluster_reads_do_not_change_ready_session(database: Database, tmp_path: Path) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    gateway.calls.clear()

    resources = manager.resources(created.id)
    observed_events = manager.events(created.id)
    logs = manager.logs("web-abc", container="web", previous=True, tail_lines=10)

    assert resources.session.status is SessionStatus.READY
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


def test_cluster_read_missing_namespace_does_not_change_session(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    gateway.exists = False

    with pytest.raises(LabManagerError) as error:
        manager.resources(created.id)

    assert error.value.code == ManagerErrorCode.ENVIRONMENT_REMOVED
    assert persisted(database, created.id).status is SessionStatus.READY


def test_hints_unlock_in_order_and_last_hint_is_idempotent(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")

    first = manager.next_hint(created.id)
    repeated = manager.next_hint(created.id)

    assert (first.level, first.newly_unlocked) == (1, True)
    assert (repeated.level, repeated.newly_unlocked) == (1, False)
    assert first.kind is HintKind.OBSERVATION
    assert repeated.request_count == 2
    assert repeated.unlocked_count == 1
    assert persisted(database, created.id).status is SessionStatus.IN_PROGRESS


def test_three_hint_layers_unlock_in_order_and_repeat_last_level(
    database: Database, tmp_path: Path
) -> None:
    registry = LabRegistry(Path(__file__).parents[1] / "labs")
    manager, _, _, _ = build_manager(database, tmp_path, registry=registry)
    created = manager.start("lab-001-deployment-scaling")

    results = [manager.next_hint(created.id) for _ in range(4)]

    assert [result.kind for result in results] == [
        HintKind.OBSERVATION,
        HintKind.COMMAND,
        HintKind.FAULT_DIRECTION,
        HintKind.FAULT_DIRECTION,
    ]
    assert [result.request_count for result in results] == [1, 2, 3, 4]
    assert [result.unlocked_count for result in results] == [1, 2, 3, 3]
    assert results[-1].newly_unlocked is False
    assert results[1].content.startswith("kubectl ")


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


def test_progress_is_derived_from_sessions_and_success_events(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    first = manager.start("complete-lab")
    manager.verify(first.id)
    manager.cleanup(first.id)
    second = manager.start("complete-lab")
    manager.verify(second.id)
    manager.cleanup(second.id)

    report = manager.progress()
    item = next(value for value in report.labs if value.lab_id == "complete-lab")

    assert item.attempt_count == 2
    assert item.completion_count == 2
    assert item.repeat_completion_count == 1
    assert item.first_completed_at is not None
    assert item.last_completed_at is not None
    assert report.categories[0].completed_lab_count == 1


def test_retrospective_metadata_and_export_are_public_bounded_and_html_safe(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    manager.next_hint(created.id)
    with database.unit_of_work() as uow:
        uow.verifications.add(
            VerificationRunInput(
                id="123e4567-e89b-42d3-a456-426614174222",
                session_id=created.id,
                purpose=VerificationPurpose.MANUAL,
                status=ValidationStatus.ERROR,
                reset_sequence=0,
                duration_ms=9,
                results=(
                    CheckResultInput(
                        check_id="pod-ready",
                        check_type="pod_status",
                        status=ValidationStatus.ERROR,
                        expected={"secret": "must-not-export"},
                        actual={"token": "must-not-export"},
                        message="Bearer private-value Traceback hidden",
                        retryable=True,
                        duration_ms=9,
                    ),
                ),
            )
        )
        uow.commit()
    manager.save_retrospective(
        RetrospectiveInput(
            symptom="<script>alert(1)</script> token=private-value",
            investigation="# injected heading",
            root_cause="Wrong image",
        ),
        created.id,
    )

    state = manager.retrospective(created.id)
    exported = manager.export_retrospective(created.id)

    assert state.metadata is not None
    assert state.metadata.hint_request_count == 1
    assert state.metadata.manual_verification_count == 1
    assert state.metadata.last_verification is not None
    assert state.metadata.last_verification.status == "unavailable"
    assert "<script>" not in exported
    assert "&lt;script&gt;" in exported
    assert "private-value" not in exported
    assert "expected" not in exported.casefold()
    assert "actual" not in exported.casefold()
    assert "开始时间" in exported
    assert "首次通过" in exported
    assert "清理时间" in exported
    assert "完成耗时" in exported
    assert "总体状态：unavailable" in exported
    assert len(exported) <= 50_000


def test_start_creates_ready_session_in_dependency_order(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, trust, validation = build_manager(database, tmp_path)

    result = manager.start("complete-lab")

    assert result.status is SessionStatus.READY
    assert result.namespace == "kubelab-complete-lab"
    assert gateway.calls == ["create", "apply", "resources", "pods"]
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


def test_status_reconciles_without_marking_learning_progress(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    manager.start("complete-lab")
    gateway.calls.clear()

    result = manager.status()

    assert result.session.status is SessionStatus.READY
    assert result.namespace_exists is True
    assert result.namespace_owned is True
    assert result.cluster_state is ClusterState.PRESENT
    assert result.stage is SessionStage.READY
    assert gateway.calls == ["exists", "owned"]


def test_session_recovery_is_sqlite_only_for_stable_and_interrupted_states(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    gateway.calls.clear()

    stable = manager.session_status_snapshot()
    with database.unit_of_work() as uow:
        uow.sessions.transition(
            created.id,
            SessionStatus.RESETTING,
            event_type="simulated_interruption",
        )
        uow.commit()
    interrupted = manager.session_status_snapshot()

    assert stable.cluster_state is ClusterState.NOT_CHECKED
    assert stable.namespace_exists is None
    assert stable.stage is SessionStage.READY
    assert interrupted.stage is SessionStage.RESETTING
    assert interrupted.workspace_command == "kubelab workspace enter"
    assert gateway.calls == []


def test_timeline_merges_events_hints_and_sanitized_evidence(
    database: Database, tmp_path: Path
) -> None:
    manager, _, _, _ = build_manager(database, tmp_path)
    created = manager.start("complete-lab")
    manager.next_hint(created.id)

    timeline = manager.timeline(created.id)

    assert timeline.session_id == created.id
    assert {entry.kind for entry in timeline.entries} >= {"session_event", "hint", "evidence"}
    assert list(timeline.entries) == sorted(
        timeline.entries,
        key=lambda item: (item.occurred_at, item.kind, item.title),
    )
    serialized = timeline.model_dump_json().casefold()
    assert "secret" not in serialized
    assert "expected" not in serialized
    assert "actual" not in serialized


def test_evidence_capture_failure_is_recorded_without_failing_start(
    database: Database, tmp_path: Path
) -> None:
    gateway = FakeGateway()
    gateway.failures["resources"] = RuntimeError("Bearer unsafe-stack")
    manager, _, _, _ = build_manager(database, tmp_path, gateway=gateway)

    created = manager.start("complete-lab")
    evidence = [entry for entry in manager.timeline(created.id).entries if entry.kind == "evidence"]

    assert created.status is SessionStatus.READY
    assert evidence[0].status == "unavailable"
    assert evidence[0].details == {"reason": "RESOURCE_SNAPSHOT_UNAVAILABLE"}
    assert "unsafe-stack" not in evidence[0].model_dump_json()


def test_workspace_uses_trusted_active_session_and_revokes_access(
    database: Database, tmp_path: Path
) -> None:
    manager, gateway, _, _ = build_manager(database, tmp_path)
    session = manager.start("complete-lab")
    gateway.calls.clear()

    access = manager.open_workspace()

    assert access.session_id == session.id
    assert persisted(database, session.id).status is SessionStatus.IN_PROGRESS
    assert gateway.calls == ["workspace"]
    assert gateway.closed is True

    gateway.closed = False
    manager.close_workspace(session.id)

    assert gateway.calls[-2:] == ["exists", "workspace-revoke"]
    assert gateway.closed is True


def test_workspace_rejects_context_drift_before_gateway(database: Database, tmp_path: Path) -> None:
    manager, gateway, trust, _ = build_manager(database, tmp_path)
    manager.start("complete-lab")
    gateway.calls.clear()
    trust.error = ContextNotTrustedError("drifted")

    with pytest.raises(LabManagerError) as error:
        manager.open_workspace()

    assert error.value.code == ManagerErrorCode.CONTEXT_DRIFT
    assert gateway.calls == []


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
    assert gateway.calls == ["delete", "create", "apply", "resources", "pods"]
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
    assert gateway.calls == ["delete", "create", "apply", "resources", "pods"]


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
    assert gateway.calls == ["resources", "pods", "delete"]


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


def test_fixed_variants_follow_baseline_coverage_retry_and_lru_order(
    database: Database, tmp_path: Path
) -> None:
    registry = LabRegistry(Path(__file__).parents[1] / "labs")
    manager, _, _, _ = build_manager(database, tmp_path, registry=registry)

    baseline = manager.start("lab-013-service-target-port")
    assert baseline.variant_id == "baseline"
    manager.verify(baseline.id)
    manager.cleanup(baseline.id)

    first_variant = manager.start("lab-013-service-target-port")
    assert first_variant.variant_id == "variant-b"
    manager.verify(first_variant.id)
    manager.cleanup(first_variant.id)

    second_variant = manager.start("lab-013-service-target-port")
    assert second_variant.variant_id == "variant-c"
    manager.cleanup(second_variant.id)

    retried = manager.start("lab-013-service-target-port")
    assert retried.variant_id == "variant-c"
    manager.verify(retried.id)
    manager.cleanup(retried.id)

    least_recently_practiced = manager.start("lab-013-service-target-port")
    assert least_recently_practiced.variant_id == "variant-b"


def test_blind_variant_is_hidden_until_success_then_revealed_everywhere(
    database: Database, tmp_path: Path
) -> None:
    registry = LabRegistry(Path(__file__).parents[1] / "labs")
    manager, _, _, _ = build_manager(database, tmp_path, registry=registry)
    baseline = manager.start("lab-013-service-target-port")
    manager.verify(baseline.id)
    manager.cleanup(baseline.id)
    variant = manager.start("lab-013-service-target-port")

    hidden = manager.show_lab("lab-013-service-target-port")
    assert hidden.practice_mode.value == "blind_repeat"
    assert hidden.scenario_revealed is False
    assert hidden.scenario_name is None
    assert hidden.root_cause is None
    assert hidden.initial_check_types == ()
    assert hidden.success_check_types == ()
    assert all(item.revealed is False for item in hidden.fault_map)
    serialized = hidden.model_dump_json()
    assert "Service命名端口错配" not in serialized
    assert "错误的命名targetPort" not in serialized
    hidden_export = manager.export_retrospective(variant.id)
    assert "Service命名端口错配" not in hidden_export
    assert "错误的命名targetPort" not in hidden_export
    assert manager.timeline(variant.id).entries[0].title == "复练场景已选择"

    hint = manager.next_hint(variant.id)
    assert hint.content.startswith("Endpoint存在时")
    manager.verify(variant.id)

    revealed = manager.show_lab("lab-013-service-target-port")
    assert revealed.scenario_revealed is True
    assert revealed.scenario_name == "Service命名端口错配"
    assert revealed.root_cause == "Service使用了错误的命名targetPort。"
    assert revealed.fault_map[0].revealed is True
    assert "scenario_revealed" in events(database, variant.id)
    assert "Service命名端口错配" in manager.export_retrospective(variant.id)

    progress = manager.progress()
    item = next(value for value in progress.labs if value.lab_id == variant.lab_id)
    assert item.baseline_completed is True
    assert item.variant_total == 2
    assert item.variant_completed == 1
    assert item.revealed_scenarios == ("Service命名端口错配",)


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
