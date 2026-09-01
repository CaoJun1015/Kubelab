"""Declarative, cluster-free Gateway and lifecycle runner for lab authors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from kubelab.authoring_schema import (
    AuthoringState,
    ConfigValueObservation,
    ContainerImageObservation,
    DeploymentAvailableObservation,
    DnsResolutionObservation,
    FakeObservation,
    HttpResponseObservation,
    LabAuthoringContract,
    PodStatusObservation,
    PvcStatusObservation,
    ResourceExistsObservation,
    ServiceEndpointObservation,
)
from kubelab.kubernetes_gateway import (
    ConfigMatchResult,
    ContainerSummary,
    DnsProbeResult,
    HttpProbeResult,
    PodSummary,
    SessionScope,
)
from kubelab.lab_registry import ExecutableLab
from kubelab.lab_schema import (
    CheckDefinition,
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
from kubelab.session_state import ValidationStatus, VerificationPurpose
from kubelab.validation_engine import ValidationEngine, ValidationRunResult


class FakeContractError(ValueError):
    """A stable author-contract error without source content or internal values."""

    def __init__(self, code: str, message: str, *, field_path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_path = field_path


@dataclass(frozen=True)
class FakeLifecycleResult:
    initial_status: ValidationStatus
    faulted_success_status: ValidationStatus
    first_repair_status: ValidationStatus | None
    repaired_status: ValidationStatus
    reset_initial_status: ValidationStatus
    reset_success_status: ValidationStatus


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class DeclarativeFakeGateway:
    """Translate check-keyed safe observations into the production Gateway protocol."""

    def __init__(self, checks: tuple[CheckDefinition, ...], state: AuthoringState) -> None:
        definitions = {check.id: check for check in checks}
        expected_ids = set(definitions)
        actual_ids = set(state.observations)
        missing = sorted(expected_ids - actual_ids)
        unknown = sorted(actual_ids - expected_ids)
        if missing:
            raise FakeContractError(
                "AUTHOR_OBSERVATION_MISSING",
                "The author state does not cover every runtime check.",
                field_path=f"observations.{missing[0]}",
            )
        if unknown:
            raise FakeContractError(
                "AUTHOR_OBSERVATION_UNKNOWN_CHECK",
                "The author state references an unknown runtime check.",
                field_path=f"observations.{unknown[0]}",
            )

        self._observations: dict[tuple[Any, ...], FakeObservation] = {}
        self._selectors: dict[tuple[Any, ...], Mapping[str, str]] = {}
        for check in checks:
            observation = state.observations[check.id]
            if observation.type != check.type:
                raise FakeContractError(
                    "AUTHOR_OBSERVATION_TYPE_MISMATCH",
                    "The observation type must match the referenced runtime check.",
                    field_path=f"observations.{check.id}.type",
                )
            key = _gateway_key(check)
            existing = self._observations.get(key)
            if existing is not None and existing != observation:
                raise FakeContractError(
                    "AUTHOR_OBSERVATION_CONFLICT",
                    "Checks sharing one Gateway query must use the same observation.",
                    field_path=f"observations.{check.id}",
                )
            self._observations[key] = observation
            if isinstance(check, PodStatusCheck):
                self._selectors[key] = check.selector

    def _get(self, key: tuple[Any, ...], expected: type[FakeObservation]) -> FakeObservation:
        try:
            value = self._observations[key]
        except KeyError as exc:  # pragma: no cover - construction normally proves coverage
            raise RuntimeError("The declarative Fake Gateway has no matching observation.") from exc
        if not isinstance(value, expected):
            raise RuntimeError("The declarative Fake Gateway observation type is invalid.")
        return value

    def resource_exists(
        self, scope: SessionScope, *, api_version: str, kind: str, name: str
    ) -> bool:
        del scope
        value = self._get(("resource_exists", api_version, kind, name), ResourceExistsObservation)
        assert isinstance(value, ResourceExistsObservation)
        return value.exists

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]:
        del scope
        key = ("pod_status", tuple(sorted(selector.items())))
        value = self._get(key, PodStatusObservation)
        assert isinstance(value, PodStatusObservation)
        labels = dict(selector)
        return tuple(
            PodSummary(
                name=pod.name,
                labels=labels,
                phase=pod.phase,
                ready=pod.ready,
                restart_count=pod.restart_count,
                containers=tuple(
                    ContainerSummary(
                        name=container.name,
                        image=container.image,
                        ready=container.ready,
                        restart_count=container.restart_count,
                        state=container.state,
                        reason=container.reason,
                    )
                    for container in pod.containers
                ),
            )
            for pod in value.pods
        )

    def deployment_available_replicas(self, scope: SessionScope, name: str) -> int | None:
        del scope
        value = self._get(("deployment_available", name), DeploymentAvailableObservation)
        assert isinstance(value, DeploymentAvailableObservation)
        return value.available_replicas

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None:
        del scope
        value = self._get(("service_endpoint_count", name), ServiceEndpointObservation)
        assert isinstance(value, ServiceEndpointObservation)
        return value.count

    def workload_container_image(
        self,
        scope: SessionScope,
        *,
        workload_kind: str,
        workload_name: str,
        container: str,
    ) -> str | None:
        del scope
        value = self._get(
            ("container_image", workload_kind, workload_name, container),
            ContainerImageObservation,
        )
        assert isinstance(value, ContainerImageObservation)
        return value.image

    def config_value_matches(
        self,
        scope: SessionScope,
        *,
        source_kind: str,
        source_name: str,
        key: str,
        expected_value: str,
    ) -> ConfigMatchResult:
        del scope
        value = self._get(
            ("config_value", source_kind, source_name, key, expected_value),
            ConfigValueObservation,
        )
        assert isinstance(value, ConfigValueObservation)
        return ConfigMatchResult(
            resource_exists=value.resource_exists,
            key_exists=value.key_exists,
            matched=value.matched,
            valid_encoding=value.valid_encoding,
        )

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None:
        del scope
        value = self._get(("pvc_status", name), PvcStatusObservation)
        assert isinstance(value, PvcStatusObservation)
        return value.phase

    def run_http_probe(
        self, scope: SessionScope, target: HttpTarget, *, deadline: float
    ) -> HttpProbeResult:
        del scope, deadline
        value = self._get(
            (
                "http_response",
                target.mode,
                target.name,
                target.port,
                target.path,
                target.scheme,
            ),
            HttpResponseObservation,
        )
        assert isinstance(value, HttpResponseObservation)
        return HttpProbeResult(
            target_available=value.target_available,
            status_code=value.status_code,
            exit_code=value.exit_code,
            infrastructure_error=value.infrastructure_error,
            timed_out=value.timed_out,
            cleanup_warning="cleanup required" if value.cleanup_warning else None,
        )

    def run_dns_probe(
        self,
        scope: SessionScope,
        *,
        service: str,
        pod: str | None,
        deadline: float,
    ) -> DnsProbeResult:
        del scope, deadline
        value = self._get(("dns_resolution", service, pod), DnsResolutionObservation)
        assert isinstance(value, DnsResolutionObservation)
        return DnsProbeResult(
            resolved=value.resolved,
            infrastructure_error=value.infrastructure_error,
            timed_out=value.timed_out,
            cleanup_warning="cleanup required" if value.cleanup_warning else None,
        )


class FakeContractRunner:
    """Prove the author lifecycle with the production validation engine and no persistence."""

    def run(self, lab: ExecutableLab, contract: LabAuthoringContract) -> FakeLifecycleResult:
        checks = tuple((*lab.definition.initial_checks, *lab.definition.success_checks))
        clock = _FakeClock()
        engine = ValidationEngine(None, monotonic=clock.monotonic, sleep=clock.sleep)
        scope = SessionScope(
            lab_id=lab.definition.metadata.id,
            session_id=str(uuid4()),
            namespace=lab.definition.environment.namespace,
            context_fingerprint="a" * 64,
        )

        faulted_gateway = DeclarativeFakeGateway(checks, contract.states.faulted)
        initial = engine.validate_initial_contract(scope, lab, faulted_gateway, reset_sequence=0)
        faulted_success = engine.validate_success_contract(
            scope,
            lab,
            faulted_gateway,
            reset_sequence=0,
            purpose=VerificationPurpose.MANUAL,
        )
        if initial.status is not ValidationStatus.PASSED:
            raise FakeContractError(
                "AUTHOR_INITIAL_CONTRACT_FAILED",
                "The faulted state must satisfy initial checks and fail success preflight.",
                field_path="states.faulted",
            )
        if faulted_success.status is not ValidationStatus.FAILED:
            raise FakeContractError(
                "AUTHOR_FAULT_NOT_REPRODUCED",
                "The faulted state must fail at least one success check without errors.",
                field_path="states.faulted",
            )

        first_repair: ValidationRunResult | None = None
        if contract.states.first_repair is not None:
            first_gateway = DeclarativeFakeGateway(checks, contract.states.first_repair)
            first_repair = engine.validate_success_contract(
                scope,
                lab,
                first_gateway,
                reset_sequence=0,
                purpose=VerificationPurpose.MANUAL,
            )
            if first_repair.status is not ValidationStatus.FAILED:
                raise FakeContractError(
                    "AUTHOR_SECOND_ROOT_CAUSE_MISSING",
                    "The first repair must leave at least one success check unmet.",
                    field_path="states.firstRepair",
                )
            before = {item.check_id: item.status for item in faulted_success.results}
            after = {item.check_id: item.status for item in first_repair.results}
            if not any(
                before[check_id] is ValidationStatus.FAILED and status is ValidationStatus.PASSED
                for check_id, status in after.items()
            ):
                raise FakeContractError(
                    "AUTHOR_FIRST_ROOT_CAUSE_NOT_PROVEN",
                    "The first repair must make at least one previously failing check pass.",
                    field_path="states.firstRepair",
                )

        repaired_gateway = DeclarativeFakeGateway(checks, contract.states.repaired)
        repaired = engine.validate_success_contract(
            scope,
            lab,
            repaired_gateway,
            reset_sequence=0,
            purpose=VerificationPurpose.MANUAL,
        )
        if repaired.status is not ValidationStatus.PASSED:
            raise FakeContractError(
                "AUTHOR_REPAIR_CONTRACT_FAILED",
                "The repaired state must satisfy every success check.",
                field_path="states.repaired",
            )

        reset_gateway = DeclarativeFakeGateway(checks, contract.states.faulted)
        reset_initial = engine.validate_initial_contract(
            scope, lab, reset_gateway, reset_sequence=1
        )
        reset_success = engine.validate_success_contract(
            scope,
            lab,
            reset_gateway,
            reset_sequence=1,
            purpose=VerificationPurpose.MANUAL,
        )
        if reset_initial.status is not ValidationStatus.PASSED or (
            reset_success.status is not ValidationStatus.FAILED
        ):
            raise FakeContractError(
                "AUTHOR_RESET_CONTRACT_FAILED",
                "Reset must restore the original reproducible fault state.",
                field_path="states.reset",
            )

        return FakeLifecycleResult(
            initial_status=initial.status,
            faulted_success_status=faulted_success.status,
            first_repair_status=first_repair.status if first_repair else None,
            repaired_status=repaired.status,
            reset_initial_status=reset_initial.status,
            reset_success_status=reset_success.status,
        )


def _gateway_key(check: CheckDefinition) -> tuple[Any, ...]:
    if isinstance(check, ResourceExistsCheck):
        return ("resource_exists", check.api_version, check.kind, check.name)
    if isinstance(check, PodStatusCheck):
        return ("pod_status", tuple(sorted(check.selector.items())))
    if isinstance(check, DeploymentAvailableCheck):
        return ("deployment_available", check.name)
    if isinstance(check, ServiceEndpointCountCheck):
        return ("service_endpoint_count", check.name)
    if isinstance(check, ContainerImageCheck):
        return (
            "container_image",
            check.workload_kind,
            check.workload_name,
            check.container,
        )
    if isinstance(check, ConfigValueCheck):
        return (
            "config_value",
            check.source_kind,
            check.source_name,
            check.key,
            check.expected_value,
        )
    if isinstance(check, PvcStatusCheck):
        return ("pvc_status", check.name)
    if isinstance(check, HttpResponseCheck):
        target = check.target
        return (
            "http_response",
            target.mode,
            target.name,
            target.port,
            target.path,
            target.scheme,
        )
    if isinstance(check, DnsResolutionCheck):
        return ("dns_resolution", check.service, check.pod)
    raise FakeContractError(
        "AUTHOR_VALIDATOR_UNSUPPORTED",
        "The scenario uses a validation type unsupported by the author runner.",
        field_path=f"checks.{check.id}",
    )


__all__ = [
    "DeclarativeFakeGateway",
    "FakeContractError",
    "FakeContractRunner",
    "FakeLifecycleResult",
]
