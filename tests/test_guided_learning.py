"""Guided-learning readiness tests use only fake read-only diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from kubelab.context_trust import ContextInspection, TrustState
from kubelab.database import Database
from kubelab.doctor import CheckStatus, DiagnosticCheck, DoctorReport, HealthStatus
from kubelab.guided_learning import (
    EnvironmentNotReadyError,
    EnvironmentReadinessService,
    ReadinessCheckStatus,
    ReadinessStatus,
    public_validation_outcome,
)
from kubelab.lab_schema import LabRequirements
from kubelab.session_state import ValidationStatus

NOW = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


class FakeDoctor:
    def __init__(self, report: DoctorReport) -> None:
        self.report = report
        self.calls = 0

    def run(self) -> DoctorReport:
        self.calls += 1
        return self.report


class FakeContextTrust:
    def __init__(self, state: TrustState = TrustState.TRUSTED) -> None:
        self.state = state
        self.error: Exception | None = None

    def inspect(self) -> ContextInspection:
        if self.error is not None:
            raise self.error
        return ContextInspection(
            context_name="minikube",
            minikube_profile="minikube",
            api_server="https://127.0.0.1:8443",
            ca_sha256="a" * 64,
            kube_system_uid="kube-system-uid",
            kubernetes_version="v1.31.2",
            trusted=self.state is TrustState.TRUSTED,
            trust_state=self.state,
        )


def diagnostic(
    check_id: str,
    status: CheckStatus = CheckStatus.PASS,
    message: str = "ready",
    **details: JsonValue,
) -> DiagnosticCheck:
    return DiagnosticCheck(id=check_id, status=status, message=message, details=details)


def healthy_report(*, cpu: float = 4, memory: int = 8192) -> DoctorReport:
    return DoctorReport(
        status=HealthStatus.HEALTHY,
        generated_at=NOW,
        checks=[
            diagnostic("runtime_platform"),
            diagnostic("ubuntu_distribution"),
            diagnostic("docker_cli"),
            diagnostic("docker_daemon"),
            diagnostic("kubectl_cli"),
            diagnostic("minikube_cli"),
            diagnostic("minikube_status"),
            diagnostic("kubeconfig"),
            diagnostic("cluster_api", version="v1.31.2"),
            diagnostic("kubectl_version_skew"),
            diagnostic("nodes_ready"),
            diagnostic("cluster_resources", cpu_cores=cpu, memory_mib=memory),
            diagnostic("default_storage_class"),
            diagnostic("ingress_addon"),
            diagnostic("metrics_server_addon"),
        ],
    )


def requirements(
    *, cpu: int = 2, memory: int = 2048, addons: tuple[str, ...] = ()
) -> LabRequirements:
    return LabRequirements(
        kubernetes=">=1.28",
        minimumCpu=cpu,
        minimumMemoryMiB=memory,
        addons=addons,
    )


def service(
    tmp_path: Path,
    report: DoctorReport,
    trust: FakeContextTrust | None = None,
) -> tuple[EnvironmentReadinessService, Database, FakeDoctor]:
    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    doctor = FakeDoctor(report)
    readiness = EnvironmentReadinessService(
        doctor=doctor,
        context_trust=trust or FakeContextTrust(),
        unit_of_work=database.unit_of_work,
    )
    return readiness, database, doctor


def test_cached_onboarding_is_pure_and_successful_check_is_persisted(tmp_path: Path) -> None:
    readiness, database, doctor = service(tmp_path, healthy_report())
    try:
        initial = readiness.cached()
        assert initial.first_use is True
        assert initial.report is None
        assert doctor.calls == 0

        report = readiness.check(requirements(addons=("ingress",)))
        cached = readiness.cached()

        assert report.status is ReadinessStatus.READY
        assert cached.first_use is False
        assert cached.completed_at == NOW
        assert cached.report == report
        assert doctor.calls == 1
    finally:
        database.dispose()


def test_untrusted_context_blocks_with_fixed_safe_remediation(tmp_path: Path) -> None:
    trust = FakeContextTrust(TrustState.UNTRUSTED)
    readiness, database, _ = service(tmp_path, healthy_report(), trust)
    try:
        try:
            readiness.assert_ready(requirements())
        except EnvironmentNotReadyError as exc:
            report = exc.report
        else:  # pragma: no cover - assertion guard
            raise AssertionError("readiness should block an untrusted Context")

        context = next(check for check in report.checks if check.id == "context_trust")
        assert report.status is ReadinessStatus.BLOCKED
        assert context.status is ReadinessCheckStatus.FAIL
        assert context.commands == ("kubelab context inspect", "kubelab context trust")
        assert "secret" not in report.model_dump_json().casefold()
    finally:
        database.dispose()


def test_requirement_checks_block_cpu_version_and_missing_addon(tmp_path: Path) -> None:
    report = healthy_report(cpu=1, memory=1024)
    report.checks[-2] = diagnostic("ingress_addon", CheckStatus.WARN)
    readiness, database, _ = service(tmp_path, report)
    try:
        result = readiness.check(
            LabRequirements(
                kubernetes=">=1.32",
                minimumCpu=2,
                minimumMemoryMiB=4096,
                addons=("ingress",),
            )
        )
        failed_ids = {
            check.id for check in result.checks if check.status is ReadinessCheckStatus.FAIL
        }
        assert {"lab_resources", "lab_kubernetes_version", "lab_addon_ingress"} <= failed_ids
        assert result.status is ReadinessStatus.BLOCKED
    finally:
        database.dispose()


def test_default_storage_class_requirement_blocks_with_fixed_remediation(tmp_path: Path) -> None:
    report = healthy_report()
    report.checks[-3] = diagnostic("default_storage_class", CheckStatus.WARN)
    readiness, database, _ = service(tmp_path, report)
    try:
        result = readiness.check(requirements(addons=("default-storageclass",)))
        storage = next(
            check for check in result.checks if check.id == "lab_addon_default-storageclass"
        )

        assert result.status is ReadinessStatus.BLOCKED
        assert storage.status is ReadinessCheckStatus.FAIL
        assert storage.commands == (
            "minikube addons enable default-storageclass",
            "minikube addons enable storage-provisioner",
        )
    finally:
        database.dispose()


def test_diagnostic_exception_is_not_exposed(tmp_path: Path) -> None:
    trust = FakeContextTrust()
    trust.error = RuntimeError("Bearer top-secret\nTraceback: unsafe")
    readiness, database, _ = service(tmp_path, healthy_report(), trust)
    try:
        result = readiness.check(requirements())
        serialized = result.model_dump_json()
        assert result.status is ReadinessStatus.BLOCKED
        assert "top-secret" not in serialized
        assert "Traceback" not in serialized
    finally:
        database.dispose()


def test_public_validation_outcome_has_exact_three_states() -> None:
    assert public_validation_outcome(ValidationStatus.PASSED).value == "passed"
    assert public_validation_outcome(ValidationStatus.FAILED).value == "failed"
    assert public_validation_outcome(ValidationStatus.ERROR).value == "unavailable"
