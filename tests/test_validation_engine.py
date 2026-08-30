"""Declarative validator, polling, aggregation, and persistence tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from kubelab.database import Database
from kubelab.db_models import CheckResultRecord, VerificationRunRecord
from kubelab.kubernetes_gateway import (
    ConfigMatchResult,
    ContainerSummary,
    DnsProbeResult,
    GatewayErrorCode,
    HttpProbeResult,
    KubernetesGatewayError,
    PodSummary,
    SessionScope,
)
from kubelab.lab_registry import LabRegistry, LoadedLab
from kubelab.lab_schema import (
    ConfigValueCheck,
    ContainerImageCheck,
    DeploymentAvailableCheck,
    DnsResolutionCheck,
    HttpResponseCheck,
    HttpTarget,
    PodStatusCheck,
    PvcStatusCheck,
    ResourceExistsCheck,
    ServiceEndpointCountCheck,
)
from kubelab.repositories import VerificationRepository
from kubelab.session_state import (
    NewLabSession,
    ValidationStatus,
)
from kubelab.validation_engine import ValidationEngine

SESSION_ID = "123e4567-e89b-42d3-a456-426614174222"
FINGERPRINT = "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _next(values: list[Any]) -> Any:
    if len(values) > 1:
        return values.pop(0)
    return values[0]


class FakeValidationGateway:
    def __init__(self) -> None:
        self.resources: list[bool] = [True]
        self.pods: list[tuple[PodSummary, ...]] = [()]
        self.deployment: list[int | None] = [1]
        self.endpoints: list[int | None] = [1]
        self.image: list[str | None] = ["nginx:1.27"]
        self.config: list[ConfigMatchResult] = [
            ConfigMatchResult(resource_exists=True, key_exists=True, matched=True)
        ]
        self.pvc: list[str | None] = ["Bound"]
        self.http: list[HttpProbeResult] = [HttpProbeResult(status_code=200, exit_code=0)]
        self.dns: list[DnsProbeResult] = [DnsProbeResult(resolved=True)]
        self.failure: Exception | None = None
        self.failures: dict[str, Exception] = {}
        self.calls: list[str] = []

    def _value(self, name: str, values: list[Any]) -> Any:
        self.calls.append(name)
        failure = self.failures.get(name, self.failure)
        if failure is not None:
            raise failure
        return _next(values)

    def resource_exists(
        self, scope: SessionScope, *, api_version: str, kind: str, name: str
    ) -> bool:
        del scope, api_version, kind, name
        return bool(self._value("resource", self.resources))

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]:
        del scope, selector
        return self._value("pods", self.pods)

    def deployment_available_replicas(self, scope: SessionScope, name: str) -> int | None:
        del scope, name
        return self._value("deployment", self.deployment)

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None:
        del scope, name
        return self._value("endpoints", self.endpoints)

    def workload_container_image(
        self,
        scope: SessionScope,
        *,
        workload_kind: str,
        workload_name: str,
        container: str,
    ) -> str | None:
        del scope, workload_kind, workload_name, container
        return self._value("image", self.image)

    def config_value_matches(
        self,
        scope: SessionScope,
        *,
        source_kind: str,
        source_name: str,
        key: str,
        expected_value: str,
    ) -> ConfigMatchResult:
        del scope, source_kind, source_name, key, expected_value
        return self._value("config", self.config)

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None:
        del scope, name
        return self._value("pvc", self.pvc)

    def run_http_probe(
        self, scope: SessionScope, target: Any, *, deadline: float
    ) -> HttpProbeResult:
        del scope, target, deadline
        return self._value("http", self.http)

    def run_dns_probe(
        self,
        scope: SessionScope,
        *,
        service: str,
        pod: str | None,
        deadline: float,
    ) -> DnsProbeResult:
        del scope, service, pod, deadline
        return self._value("dns", self.dns)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "state" / "kubelab.db")
    value.initialize()
    with value.unit_of_work() as uow:
        uow.sessions.create(
            NewLabSession(
                id=SESSION_ID,
                lab_id="complete-lab",
                namespace="kubelab-complete-lab",
                context_name="minikube",
                context_fingerprint=FINGERPRINT,
            )
        )
        uow.commit()
    yield value
    value.dispose()


@pytest.fixture
def loaded_lab() -> LoadedLab:
    root = Path(__file__).parent / "fixtures" / "labs" / "valid"
    return LabRegistry(root).scan().labs[0]


def scope() -> SessionScope:
    return SessionScope(
        lab_id="complete-lab",
        session_id=SESSION_ID,
        namespace="kubelab-complete-lab",
        context_fingerprint=FINGERPRINT,
    )


def with_checks(loaded: LoadedLab, *checks: Any) -> LoadedLab:
    definition = loaded.definition.model_copy(update={"success_checks": tuple(checks)})
    return loaded.model_copy(update={"definition": definition})


def test_dns_resolution_check_uses_only_public_boolean_outcome(
    database: Database, loaded_lab: LoadedLab
) -> None:
    check = DnsResolutionCheck(
        id="stable-dns",
        type="dns_resolution",
        service="web-headless",
        pod="web-0",
        expectedResolved=True,
        timeoutSeconds=10,
        unmetMessage="Stable DNS is unavailable.",
    )
    gateway = FakeValidationGateway()
    gateway.dns = [DnsProbeResult(resolved=True)]
    clock = FakeClock()
    engine = ValidationEngine(
        database.unit_of_work,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = engine.validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, reset_sequence=0
    )

    assert result.status is ValidationStatus.PASSED
    assert result.results[0].check_type == "dns_resolution"
    assert "cluster.local" not in result.model_dump_json()


def test_dns_probe_infrastructure_failure_is_unavailable(
    database: Database, loaded_lab: LoadedLab
) -> None:
    check = DnsResolutionCheck(
        id="stable-dns",
        type="dns_resolution",
        service="web-headless",
        pod="web-0",
        expectedResolved=True,
        timeoutSeconds=10,
        unmetMessage="Stable DNS is unavailable.",
    )
    gateway = FakeValidationGateway()
    gateway.dns = [DnsProbeResult(infrastructure_error=True, timed_out=True)]
    clock = FakeClock()
    engine = ValidationEngine(
        database.unit_of_work,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = engine.validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, reset_sequence=0
    )

    assert result.status is ValidationStatus.ERROR
    assert result.results[0].message == "Validation could not be completed."


def engine(database: Database, clock: FakeClock) -> ValidationEngine:
    return ValidationEngine(
        database.unit_of_work,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 26, tzinfo=UTC),
    )


def common(check_id: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "timeoutSeconds": 1,
        "unmetMessage": f"{check_id} unmet",
    }


def all_checks() -> tuple[Any, ...]:
    return (
        ResourceExistsCheck(
            **common("resource-exists"),
            type="resource_exists",
            apiVersion="v1",
            kind="Pod",
            name="web",
        ),
        PodStatusCheck(
            **common("pod-status"),
            type="pod_status",
            selector={"app": "web"},
            expectedPhase="Running",
            minimumCount=1,
            minimumReady=1,
            ready=True,
        ),
        DeploymentAvailableCheck(
            **common("deployment-available"),
            type="deployment_available",
            name="web",
            minimumReplicas=1,
        ),
        ServiceEndpointCountCheck(
            **common("service-endpoints"),
            type="service_endpoint_count",
            name="web",
            minimum=1,
        ),
        ContainerImageCheck(
            **common("container-image"),
            type="container_image",
            workloadKind="Deployment",
            workloadName="web",
            container="web",
            expectedImage="nginx:1.27",
        ),
        ConfigValueCheck(
            **common("config-value"),
            type="config_value",
            sourceKind="Secret",
            sourceName="credential",
            key="token",
            expectedValue="TOP-SECRET-VALUE",
        ),
        PvcStatusCheck(
            **common("pvc-status"),
            type="pvc_status",
            name="data",
            expectedPhase="Bound",
        ),
        HttpResponseCheck(
            **common("http-response"),
            type="http_response",
            target=HttpTarget(mode="service", name="web", port=80),
            expectedStatus=200,
        ),
    )


def ready_pod(
    *,
    phase: str = "Running",
    ready: bool = True,
    reason: str | None = None,
    restarts: int = 0,
) -> PodSummary:
    return PodSummary(
        name="web-abc",
        labels={"app": "web"},
        phase=phase,
        ready=ready,
        restart_count=restarts,
        node_name="minikube",
        reason=reason,
        containers=(
            ContainerSummary(
                name="web",
                image="nginx:1.27",
                ready=ready,
                restart_count=restarts,
                state="waiting" if reason else "running",
                reason=reason,
            ),
        ),
    )


def make_validator_unmet(gateway: FakeValidationGateway, index: int) -> None:
    if index == 0:
        gateway.resources = [False]
    elif index == 1:
        gateway.pods = [()]
    elif index == 2:
        gateway.deployment = [0]
    elif index == 3:
        gateway.endpoints = [0]
    elif index == 4:
        gateway.image = ["nginx:wrong"]
    elif index == 5:
        gateway.config = [ConfigMatchResult(resource_exists=True, key_exists=True, matched=False)]
    elif index == 6:
        gateway.pvc = ["Pending"]
    else:
        gateway.http = [HttpProbeResult(status_code=503, exit_code=0)]


def test_all_eight_validators_pass_and_persist_atomically(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.pods = [(ready_pod(),)]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, *all_checks()), gateway, 3
    )

    assert result.status is ValidationStatus.PASSED
    assert result.reset_sequence == 3
    assert len(result.results) == 8
    assert not hasattr(result.results[0], "expected")
    with database.session_factory() as session:
        run = session.get(VerificationRunRecord, result.id)
        rows = session.scalars(
            select(CheckResultRecord).where(CheckResultRecord.run_id == result.id)
        ).all()
    assert run is not None and run.status == "passed"
    assert len(rows) == 8
    serialized = repr([(row.expected, row.actual) for row in rows])
    assert "TOP-SECRET-VALUE" not in serialized


def test_verification_run_and_results_roll_back_together(
    database: Database,
    loaded_lab: LoadedLab,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = VerificationRepository.add

    def fail_after_staging(repository: VerificationRepository, run: Any) -> None:
        original(repository, run)
        raise RuntimeError("simulated commit boundary failure")

    monkeypatch.setattr(VerificationRepository, "add", fail_after_staging)
    clock = FakeClock()
    gateway = FakeValidationGateway()

    with pytest.raises(RuntimeError, match="commit boundary"):
        engine(database, clock).validate_success_contract(
            scope(), with_checks(loaded_lab, all_checks()[0]), gateway, 0
        )

    with database.session_factory() as session:
        assert session.scalar(select(VerificationRunRecord)) is None
        assert session.scalar(select(CheckResultRecord)) is None


@pytest.mark.parametrize("index", range(8))
def test_each_validator_polls_to_deadline_when_condition_is_unmet(
    database: Database, loaded_lab: LoadedLab, index: int
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    check = all_checks()[index]
    make_validator_unmet(gateway, index)

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is ValidationStatus.FAILED
    assert result.results[0].message == check.unmet_message
    assert clock.value == check.timeout_seconds


@pytest.mark.parametrize("index", range(8))
def test_each_validator_preserves_kubernetes_errors(
    database: Database, loaded_lab: LoadedLab, index: int
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.failure = KubernetesGatewayError(
        GatewayErrorCode.TIMEOUT, "API response with TOKEN", retryable=True
    )

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, all_checks()[index]), gateway, 0
    )

    assert result.status is ValidationStatus.ERROR
    assert result.results[0].retryable is True
    assert "TOKEN" not in result.results[0].message


def test_unmet_check_polls_with_bounded_backoff_then_fails(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.resources = [False]
    check = all_checks()[0]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is ValidationStatus.FAILED
    assert result.results[0].message == "resource-exists unmet"
    assert clock.sleeps == [0.5, 0.5]


def test_global_deadline_bounds_the_full_sequential_run(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.resources = [False]
    checks = tuple(
        ResourceExistsCheck(
            **common(f"missing-{index}"),
            type="resource_exists",
            apiVersion="v1",
            kind="Pod",
            name=f"missing-{index}",
        )
        for index in range(8)
    )

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, *checks), gateway, 0
    )

    assert result.status is ValidationStatus.FAILED
    assert clock.value == 6


def test_gateway_error_is_not_misreported_as_failed(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.failure = KubernetesGatewayError(
        GatewayErrorCode.FORBIDDEN, "TOKEN private", retryable=False
    )

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, all_checks()[2]), gateway, 0
    )

    assert result.status is ValidationStatus.ERROR
    assert result.results[0].retryable is False
    assert "private" not in result.results[0].message


def test_pod_waiting_reason_and_restart_bounds_are_precise(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.pods = [
        (ready_pod(phase="Pending", ready=False, reason="ImagePullBackOff", restarts=2),)
    ]
    check = PodStatusCheck(
        **common("image-pull"),
        type="pod_status",
        selector={"app": "web"},
        expectedPhase="Pending",
        minimumCount=1,
        ready=False,
        containerName="web",
        expectedWaitingReasons=("ErrImagePull", "ImagePullBackOff"),
        minimumRestartCount=1,
        maximumRestartCount=3,
    )

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is ValidationStatus.PASSED


def test_stable_window_resets_after_condition_breaks(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.pods = [
        (ready_pod(),),
        (ready_pod(ready=False),),
        (ready_pod(),),
        (ready_pod(),),
    ]
    check = PodStatusCheck(
        id="stable-ready",
        type="pod_status",
        selector={"app": "web"},
        expectedPhase="Running",
        minimumCount=1,
        minimumReady=1,
        stableSeconds=1,
        timeoutSeconds=4,
        unmetMessage="not stable",
    )

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is ValidationStatus.PASSED
    assert gateway.calls.count("pods") == 4


def test_invalid_secret_encoding_is_error_and_value_is_never_persisted(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.config = [
        ConfigMatchResult(
            resource_exists=True,
            key_exists=True,
            matched=False,
            valid_encoding=False,
        )
    ]
    check = all_checks()[5]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is ValidationStatus.ERROR
    with database.session_factory() as session:
        row = session.scalar(select(CheckResultRecord).where(CheckResultRecord.run_id == result.id))
    assert row is not None
    assert "TOP-SECRET-VALUE" not in repr((row.expected, row.actual, row.message))


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        (HttpProbeResult(status_code=503, exit_code=0), ValidationStatus.FAILED),
        (HttpProbeResult(status_code=None, exit_code=6), ValidationStatus.FAILED),
        (
            HttpProbeResult(infrastructure_error=True, timed_out=True),
            ValidationStatus.ERROR,
        ),
    ],
)
def test_http_outcomes_distinguish_failed_and_error(
    database: Database,
    loaded_lab: LoadedLab,
    probe: HttpProbeResult,
    expected: ValidationStatus,
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.http = [probe]
    check = all_checks()[7]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, check), gateway, 0
    )

    assert result.status is expected


def test_late_probe_error_does_not_override_observed_business_failure(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()

    class DeadlineProbeGateway(FakeValidationGateway):
        def run_http_probe(
            self, scope: SessionScope, target: Any, *, deadline: float
        ) -> HttpProbeResult:
            del scope, target
            if self.calls.count("http") == 0:
                self.calls.append("http")
                return HttpProbeResult(status_code=None, exit_code=7)
            self.calls.append("http")
            clock.value = deadline
            return HttpProbeResult(infrastructure_error=True, timed_out=True)

    gateway = DeadlineProbeGateway()

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, all_checks()[7]), gateway, 0
    )

    assert result.status is ValidationStatus.FAILED
    assert result.results[0].message == all_checks()[7].unmet_message


def test_probe_cleanup_warning_is_nonfatal(database: Database, loaded_lab: LoadedLab) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.http = [
        HttpProbeResult(
            status_code=200,
            exit_code=0,
            cleanup_warning="safe warning without response",
        )
    ]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, all_checks()[7]), gateway, 0
    )

    assert result.status is ValidationStatus.PASSED
    assert "cleanup" in result.results[0].message.lower()


def test_probe_cleanup_warning_is_kept_on_failed_check(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.http = [
        HttpProbeResult(
            status_code=503,
            exit_code=0,
            cleanup_warning="safe warning without response",
        )
    ]

    result = engine(database, clock).validate_success_contract(
        scope(), with_checks(loaded_lab, all_checks()[7]), gateway, 0
    )

    assert result.status is ValidationStatus.FAILED
    assert "cleanup" in result.results[0].message.lower()


def test_aggregate_error_has_priority_over_failed(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.resources = [False]
    gateway.config = [
        ConfigMatchResult(
            resource_exists=True, key_exists=True, matched=False, valid_encoding=False
        )
    ]

    result = engine(database, clock).validate_success_contract(
        scope(),
        with_checks(loaded_lab, all_checks()[0], all_checks()[5]),
        gateway,
        0,
    )

    assert [item.status for item in result.results] == [
        ValidationStatus.FAILED,
        ValidationStatus.ERROR,
    ]
    assert result.status is ValidationStatus.ERROR


def test_initial_contract_persists_initial_and_failed_success_preflight(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.deployment = [1]
    gateway.endpoints = [0]

    result = engine(database, clock).validate_initial_contract(
        scope(), loaded_lab, gateway, reset_sequence=2
    )

    assert result.status is ValidationStatus.PASSED
    with database.session_factory() as session:
        runs = session.scalars(select(VerificationRunRecord)).all()
    statuses = {run.purpose: run.status for run in runs}
    assert statuses == {"initial": "passed", "success_contract": "failed"}
    assert all(run.reset_sequence == 2 for run in runs)


def test_initial_contract_rejects_fault_that_is_already_fixed(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.deployment = [1]
    gateway.endpoints = [1]

    result = engine(database, clock).validate_initial_contract(
        scope(), loaded_lab, gateway, reset_sequence=0
    )

    assert result.status is ValidationStatus.FAILED
    assert result.error_code == "FAULT_NOT_REPRODUCED"


def test_initial_contract_preserves_success_preflight_error_retryability(
    database: Database, loaded_lab: LoadedLab
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.deployment = [1]
    gateway.failures["endpoints"] = KubernetesGatewayError(
        GatewayErrorCode.FORBIDDEN, "Access forbidden", retryable=False
    )

    result = engine(database, clock).validate_initial_contract(
        scope(), loaded_lab, gateway, reset_sequence=0
    )

    assert result.status is ValidationStatus.ERROR
    assert result.error_code == "SUCCESS_PREFLIGHT_ERROR"
    assert result.retryable is False


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_code", "expected_retryable"),
    [
        (None, ValidationStatus.FAILED, "INITIAL_CHECKS_NOT_SATISFIED", False),
        (
            KubernetesGatewayError(GatewayErrorCode.TIMEOUT, "API unavailable", retryable=True),
            ValidationStatus.ERROR,
            "INITIAL_CHECKS_NOT_SATISFIED",
            True,
        ),
        (
            KubernetesGatewayError(GatewayErrorCode.FORBIDDEN, "Access forbidden", retryable=False),
            ValidationStatus.ERROR,
            "INITIAL_CHECKS_NOT_SATISFIED",
            False,
        ),
    ],
)
def test_initial_contract_stops_when_initial_checks_do_not_pass(
    database: Database,
    loaded_lab: LoadedLab,
    failure: KubernetesGatewayError | None,
    expected_status: ValidationStatus,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    clock = FakeClock()
    gateway = FakeValidationGateway()
    gateway.deployment = [0]
    gateway.failure = failure

    result = engine(database, clock).validate_initial_contract(
        scope(), loaded_lab, gateway, reset_sequence=0
    )

    assert result.status is expected_status
    assert result.error_code == expected_code
    assert result.retryable is expected_retryable
    with database.session_factory() as session:
        runs = session.scalars(select(VerificationRunRecord)).all()
    assert len(runs) == 1
    assert runs[0].purpose == "initial"
