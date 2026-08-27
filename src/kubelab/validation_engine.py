"""Declarative validation execution, polling, aggregation, and persistence."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from kubelab.kubernetes_gateway import (
    ConfigMatchResult,
    HttpProbeResult,
    KubernetesGatewayError,
    PodSummary,
    SessionScope,
)
from kubelab.lab_registry import LoadedLab
from kubelab.lab_schema import (
    CheckDefinition,
    ConfigValueCheck,
    ContainerImageCheck,
    DeploymentAvailableCheck,
    HttpResponseCheck,
    HttpTarget,
    PodStatusCheck,
    PvcStatusCheck,
    ResourceExistsCheck,
    ServiceEndpointCountCheck,
)
from kubelab.repositories import SqlAlchemyUnitOfWork
from kubelab.session_state import (
    CheckResultInput,
    ValidationStatus,
    VerificationPurpose,
    VerificationRunInput,
)


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicCheckResult(ValidationModel):
    check_id: str
    check_type: str
    status: ValidationStatus
    message: str
    retryable: bool
    duration_ms: int


class InitialContractResult(ValidationModel):
    status: ValidationStatus
    error_code: str | None = None
    retryable: bool = False


class ValidationRunResult(ValidationModel):
    id: str
    session_id: str
    purpose: VerificationPurpose
    status: ValidationStatus
    reset_sequence: int
    checked_at: datetime
    duration_ms: int
    results: tuple[PublicCheckResult, ...]


class ValidationGateway(Protocol):
    def resource_exists(
        self, scope: SessionScope, *, api_version: str, kind: str, name: str
    ) -> bool: ...

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]: ...

    def deployment_available_replicas(self, scope: SessionScope, name: str) -> int | None: ...

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None: ...

    def workload_container_image(
        self,
        scope: SessionScope,
        *,
        workload_kind: str,
        workload_name: str,
        container: str,
    ) -> str | None: ...

    def config_value_matches(
        self,
        scope: SessionScope,
        *,
        source_kind: str,
        source_name: str,
        key: str,
        expected_value: str,
    ) -> ConfigMatchResult: ...

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None: ...

    def run_http_probe(
        self, scope: SessionScope, target: HttpTarget, *, deadline: float
    ) -> HttpProbeResult: ...


class _Observation(ValidationModel):
    satisfied: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    error_code: str | None = None
    retryable: bool = True
    warning: str | None = None


class _Evaluation(ValidationModel):
    check_id: str
    check_type: str
    status: ValidationStatus
    expected: dict[str, Any]
    actual: dict[str, Any]
    message: str
    retryable: bool
    duration_ms: int


class ValidationEngine:
    """Run strict v1alpha1 checks without exposing repositories to validators."""

    def __init__(
        self,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work = unit_of_work
        self._monotonic = monotonic
        self._sleep = sleep
        self._now = now

    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
    ) -> InitialContractResult:
        initial = self._run(
            scope,
            lab.definition.initial_checks,
            gateway,
            reset_sequence=reset_sequence,
            purpose=VerificationPurpose.INITIAL,
        )
        if initial.status is not ValidationStatus.PASSED:
            return InitialContractResult(
                status=initial.status,
                error_code="INITIAL_CHECKS_NOT_SATISFIED",
                retryable=_run_is_retryable(initial),
            )
        success_preflight = self._run(
            scope,
            lab.definition.success_checks,
            gateway,
            reset_sequence=reset_sequence,
            purpose=VerificationPurpose.SUCCESS_CONTRACT,
        )
        if success_preflight.status is ValidationStatus.ERROR:
            return InitialContractResult(
                status=ValidationStatus.ERROR,
                error_code="SUCCESS_PREFLIGHT_ERROR",
                retryable=_run_is_retryable(success_preflight),
            )
        if success_preflight.status is ValidationStatus.PASSED:
            return InitialContractResult(
                status=ValidationStatus.FAILED,
                error_code="FAULT_NOT_REPRODUCED",
            )
        return InitialContractResult(status=ValidationStatus.PASSED)

    def validate_success_contract(
        self,
        scope: SessionScope,
        lab: LoadedLab,
        gateway: ValidationGateway,
        reset_sequence: int,
        purpose: VerificationPurpose = VerificationPurpose.MANUAL,
    ) -> ValidationRunResult:
        return self._run(
            scope,
            lab.definition.success_checks,
            gateway,
            reset_sequence=reset_sequence,
            purpose=purpose,
        )

    def _run(
        self,
        scope: SessionScope,
        checks: Sequence[CheckDefinition],
        gateway: ValidationGateway,
        *,
        reset_sequence: int,
        purpose: VerificationPurpose,
    ) -> ValidationRunResult:
        started = self._monotonic()
        checked_at = self._now()
        global_deadline = started + max(check.timeout_seconds for check in checks) + 5
        evaluations = tuple(
            self._evaluate(
                scope,
                check,
                gateway,
                global_deadline=global_deadline,
            )
            for check in checks
        )
        status = _aggregate(item.status for item in evaluations)
        duration_ms = _duration_ms(started, self._monotonic())
        run_id = str(uuid4())
        persistence = VerificationRunInput(
            id=run_id,
            session_id=scope.session_id,
            purpose=purpose,
            status=status,
            reset_sequence=reset_sequence,
            checked_at=checked_at,
            duration_ms=duration_ms,
            results=tuple(
                CheckResultInput(
                    check_id=item.check_id,
                    check_type=item.check_type,
                    status=item.status,
                    expected=item.expected,
                    actual=item.actual,
                    message=item.message,
                    retryable=item.retryable,
                    duration_ms=item.duration_ms,
                )
                for item in evaluations
            ),
        )
        with self._unit_of_work() as uow:
            uow.verifications.add(persistence)
            uow.commit()
        return ValidationRunResult(
            id=run_id,
            session_id=scope.session_id,
            purpose=purpose,
            status=status,
            reset_sequence=reset_sequence,
            checked_at=checked_at,
            duration_ms=duration_ms,
            results=tuple(
                PublicCheckResult(
                    check_id=item.check_id,
                    check_type=item.check_type,
                    status=item.status,
                    message=item.message,
                    retryable=item.retryable,
                    duration_ms=item.duration_ms,
                )
                for item in evaluations
            ),
        )

    def _evaluate(
        self,
        scope: SessionScope,
        check: CheckDefinition,
        gateway: ValidationGateway,
        *,
        global_deadline: float,
    ) -> _Evaluation:
        started = self._monotonic()
        deadline = min(started + check.timeout_seconds, global_deadline)
        if started >= deadline:
            return _Evaluation(
                check_id=check.id,
                check_type=check.type,
                status=ValidationStatus.FAILED,
                expected={},
                actual={},
                message=check.unmet_message,
                retryable=True,
                duration_ms=0,
            )
        interval = 0.5
        stable_started: float | None = None
        last = _Observation(satisfied=False, expected={}, actual={})
        last_non_error: _Observation | None = None
        while True:
            try:
                last = self._observe(scope, check, gateway, deadline=deadline)
            except KubernetesGatewayError as exc:
                return _Evaluation(
                    check_id=check.id,
                    check_type=check.type,
                    status=ValidationStatus.ERROR,
                    expected={},
                    actual={"error_code": exc.code.value},
                    message="Validation could not query the Kubernetes environment.",
                    retryable=exc.retryable,
                    duration_ms=_duration_ms(started, self._monotonic()),
                )
            except Exception:
                return _Evaluation(
                    check_id=check.id,
                    check_type=check.type,
                    status=ValidationStatus.ERROR,
                    expected={},
                    actual={"error_code": "VALIDATOR_ERROR"},
                    message="Validation could not be completed.",
                    retryable=False,
                    duration_ms=_duration_ms(started, self._monotonic()),
                )

            if last.error_code is not None:
                if (
                    last_non_error is not None
                    and not last_non_error.satisfied
                    and self._monotonic() >= deadline
                ):
                    return self._finished(
                        check,
                        last_non_error,
                        ValidationStatus.FAILED,
                        check.unmet_message,
                        started,
                    )
                return self._finished(
                    check,
                    last,
                    ValidationStatus.ERROR,
                    "Validation could not be completed.",
                    started,
                )
            last_non_error = last
            if last.satisfied:
                stable_seconds = (
                    check.stable_seconds
                    if isinstance(check, PodStatusCheck) and check.stable_seconds
                    else 0
                )
                if stable_seconds == 0:
                    message = "Check passed."
                    return self._finished(check, last, ValidationStatus.PASSED, message, started)
                if stable_started is None:
                    stable_started = self._monotonic()
                if self._monotonic() - stable_started >= stable_seconds:
                    return self._finished(
                        check, last, ValidationStatus.PASSED, "Check passed.", started
                    )
            else:
                stable_started = None

            now = self._monotonic()
            if now >= deadline:
                return self._finished(
                    check,
                    last,
                    ValidationStatus.FAILED,
                    check.unmet_message,
                    started,
                )
            self._sleep(min(interval, max(0.0, deadline - now)))
            interval = min(interval * 2, 2.0)

    def _finished(
        self,
        check: CheckDefinition,
        observation: _Observation,
        status: ValidationStatus,
        message: str,
        started: float,
    ) -> _Evaluation:
        if observation.warning:
            message += " Probe cleanup requires Namespace cleanup."
        return _Evaluation(
            check_id=check.id,
            check_type=check.type,
            status=status,
            expected=observation.expected,
            actual=observation.actual,
            message=message,
            retryable=observation.retryable,
            duration_ms=_duration_ms(started, self._monotonic()),
        )

    def _observe(
        self,
        scope: SessionScope,
        check: CheckDefinition,
        gateway: ValidationGateway,
        *,
        deadline: float,
    ) -> _Observation:
        if isinstance(check, ResourceExistsCheck):
            exists = gateway.resource_exists(
                scope,
                api_version=check.api_version,
                kind=check.kind,
                name=check.name,
            )
            return _Observation(
                satisfied=exists,
                expected={"exists": True},
                actual={"exists": exists},
            )
        if isinstance(check, PodStatusCheck):
            return _pod_observation(check, gateway.validation_pods(scope, check.selector))
        if isinstance(check, DeploymentAvailableCheck):
            available = gateway.deployment_available_replicas(scope, check.name)
            return _Observation(
                satisfied=available is not None and available >= check.minimum_replicas,
                expected={"minimum_replicas": check.minimum_replicas},
                actual={"available_replicas": available},
            )
        if isinstance(check, ServiceEndpointCountCheck):
            count = gateway.service_endpoint_count(scope, check.name)
            return _Observation(
                satisfied=count is not None and _endpoint_constraint(check, count),
                expected={
                    "minimum": check.minimum,
                    "maximum": check.maximum,
                    "exactly": check.exactly,
                },
                actual={"count": count},
            )
        if isinstance(check, ContainerImageCheck):
            image = gateway.workload_container_image(
                scope,
                workload_kind=check.workload_kind,
                workload_name=check.workload_name,
                container=check.container,
            )
            return _Observation(
                satisfied=image == check.expected_image,
                expected={"image": check.expected_image},
                actual={"image": image},
            )
        if isinstance(check, ConfigValueCheck):
            config_result = gateway.config_value_matches(
                scope,
                source_kind=check.source_kind,
                source_name=check.source_name,
                key=check.key,
                expected_value=check.expected_value,
            )
            return _config_observation(check, config_result)
        if isinstance(check, PvcStatusCheck):
            phase = gateway.pvc_phase(scope, check.name)
            return _Observation(
                satisfied=phase == check.expected_phase,
                expected={"phase": check.expected_phase},
                actual={"phase": phase},
            )
        if isinstance(check, HttpResponseCheck):
            probe_result = gateway.run_http_probe(scope, check.target, deadline=deadline)
            return _http_observation(check, probe_result)
        return _Observation(
            satisfied=False,
            expected={},
            actual={},
            error_code="VALIDATOR_NOT_REGISTERED",
            retryable=False,
        )


def _pod_observation(check: PodStatusCheck, pods: tuple[PodSummary, ...]) -> _Observation:
    qualifying: list[PodSummary] = []
    reasons: dict[str, str | None] = {}
    restarts: dict[str, int] = {}
    for pod in pods:
        container = next(
            (item for item in pod.containers if item.name == check.container_name), None
        )
        target_ready = container.ready if container is not None else pod.ready
        matches = pod.phase == check.expected_phase
        if check.ready is not None:
            matches = matches and target_ready is check.ready
        if check.container_name is not None and container is None:
            matches = False
        if container is not None:
            reasons[pod.name] = container.reason
            restarts[pod.name] = container.restart_count
            if check.expected_waiting_reasons is not None:
                matches = (
                    matches
                    and container.state == "waiting"
                    and (container.reason in check.expected_waiting_reasons)
                )
            if check.minimum_restart_count is not None:
                matches = matches and container.restart_count >= check.minimum_restart_count
            if check.maximum_restart_count is not None:
                matches = matches and container.restart_count <= check.maximum_restart_count
        if matches:
            qualifying.append(pod)
    ready_count = sum(1 for pod in pods if pod.ready)
    ready_satisfied = check.minimum_ready is None or ready_count >= check.minimum_ready
    return _Observation(
        satisfied=len(qualifying) >= check.minimum_count and ready_satisfied,
        expected={
            "phase": check.expected_phase,
            "minimum_count": check.minimum_count,
            "minimum_ready": check.minimum_ready,
            "ready": check.ready,
            "waiting_reasons": check.expected_waiting_reasons,
            "minimum_restart_count": check.minimum_restart_count,
            "maximum_restart_count": check.maximum_restart_count,
        },
        actual={
            "selected_count": len(pods),
            "matching_count": len(qualifying),
            "ready_count": ready_count,
            "waiting_reasons": reasons,
            "restart_counts": restarts,
        },
    )


def _endpoint_constraint(check: ServiceEndpointCountCheck, count: int) -> bool:
    if check.exactly is not None:
        return count == check.exactly
    return (check.minimum is None or count >= check.minimum) and (
        check.maximum is None or count <= check.maximum
    )


def _config_observation(check: ConfigValueCheck, result: ConfigMatchResult) -> _Observation:
    safe = {
        "source_kind": check.source_kind,
        "source_name": check.source_name,
        "key": check.key,
    }
    actual = {
        "resource_exists": result.resource_exists,
        "key_exists": result.key_exists,
        "matched": result.matched,
    }
    return _Observation(
        satisfied=result.matched and result.valid_encoding,
        expected=safe,
        actual=actual,
        error_code=None if result.valid_encoding else "CONFIG_VALUE_INVALID_ENCODING",
        retryable=result.valid_encoding,
    )


def _http_observation(check: HttpResponseCheck, result: HttpProbeResult) -> _Observation:
    actual = {
        "target_available": result.target_available,
        "status_code": result.status_code,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }
    return _Observation(
        satisfied=(
            result.target_available
            and not result.infrastructure_error
            and result.exit_code == 0
            and result.status_code == check.expected_status
        ),
        expected={
            "mode": check.target.mode,
            "name": check.target.name,
            "port": check.target.port,
            "path": check.target.path,
            "status_code": check.expected_status,
        },
        actual=actual,
        error_code="HTTP_PROBE_ERROR" if result.infrastructure_error else None,
        retryable=True,
        warning=result.cleanup_warning,
    )


def _aggregate(statuses: Iterable[ValidationStatus]) -> ValidationStatus:
    values = tuple(statuses)
    if ValidationStatus.ERROR in values:
        return ValidationStatus.ERROR
    if ValidationStatus.FAILED in values:
        return ValidationStatus.FAILED
    return ValidationStatus.PASSED


def _run_is_retryable(run: ValidationRunResult) -> bool:
    return any(
        result.status is ValidationStatus.ERROR and result.retryable for result in run.results
    )


def _duration_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1000))


__all__ = [
    "InitialContractResult",
    "PublicCheckResult",
    "ValidationEngine",
    "ValidationGateway",
    "ValidationRunResult",
]
