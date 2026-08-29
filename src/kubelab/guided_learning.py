"""Guided-learning readiness models and application service."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from kubelab.context_trust import ContextInspection, TrustState
from kubelab.doctor import CheckStatus, DiagnosticCheck, DoctorReport, DoctorService
from kubelab.lab_schema import LabRequirements
from kubelab.redaction import redact_json
from kubelab.repositories import SqlAlchemyUnitOfWork
from kubelab.session_state import ValidationStatus


class ReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ReadinessCheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class PublicValidationOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class GuidedLearningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadinessCheck(GuidedLearningModel):
    id: str
    status: ReadinessCheckStatus
    message: str
    remediation: str | None = None
    commands: tuple[str, ...] = ()


class EnvironmentReadinessReport(GuidedLearningModel):
    status: ReadinessStatus
    checks: tuple[ReadinessCheck, ...]
    generated_at: datetime


class OnboardingState(GuidedLearningModel):
    first_use: bool
    completed_at: datetime | None
    report: EnvironmentReadinessReport | None


class EnvironmentNotReadyError(RuntimeError):
    code = "ENVIRONMENT_NOT_READY"
    retryable = True

    def __init__(self, report: EnvironmentReadinessReport) -> None:
        self.report = report
        self.context = {
            "status": report.status.value,
            "blocking_check_count": sum(
                check.status is ReadinessCheckStatus.FAIL for check in report.checks
            ),
        }
        super().__init__("The local environment does not satisfy this lab's requirements.")


class DoctorRunner(Protocol):
    def run(self) -> DoctorReport: ...


class ContextInspector(Protocol):
    def inspect(self) -> ContextInspection: ...


_FIXED_REMEDIATION: dict[str, tuple[str, tuple[str, ...]]] = {
    "runtime_platform": ("在WSL2 Ubuntu中运行KubeLab。", ()),
    "ubuntu_distribution": ("切换到Ubuntu WSL2发行版。", ()),
    "docker_cli": ("按照部署手册安装Docker Engine。", ()),
    "docker_daemon": (
        "启动Docker Engine并重新检查。",
        ("sudo systemctl start docker", "docker info"),
    ),
    "kubectl_cli": ("按照部署手册安装匹配版本的kubectl。", ()),
    "minikube_cli": ("按照部署手册安装minikube。", ()),
    "minikube_status": (
        "启动固定的本地minikube配置。",
        ("minikube start --driver=docker --cpus=2 --memory=4096",),
    ),
    "kubeconfig": ("启动minikube以生成本地kubeconfig。", ("minikube start",)),
    "cluster_api": (
        "检查minikube状态和当前Context。",
        ("minikube status", "kubectl config current-context"),
    ),
    "kubectl_version_skew": ("安装与API Server主版本相同、相差不超过一个次版本的kubectl。", ()),
    "nodes_ready": ("检查本地节点状态。", ("kubectl get nodes",)),
    "cluster_resources": (
        "使用足够的CPU和内存重新配置minikube。",
        ("minikube config set cpus 2", "minikube config set memory 4096"),
    ),
    "default_storage_class": (
        "启用minikube默认StorageClass。",
        (
            "minikube addons enable default-storageclass",
            "minikube addons enable storage-provisioner",
        ),
    ),
    "ingress_addon": ("启用minikube ingress Addon。", ("minikube addons enable ingress",)),
    "metrics_server_addon": (
        "启用minikube metrics-server Addon。",
        ("minikube addons enable metrics-server",),
    ),
    "context_trust": (
        "核验后显式信任当前本地minikube Context。",
        ("kubelab context inspect", "kubelab context trust"),
    ),
    "lab_kubernetes_version": ("升级本地minikube Kubernetes版本后重新检查。", ()),
}


class EnvironmentReadinessService:
    """Run explicit read-only checks and persist only their public representation."""

    def __init__(
        self,
        *,
        doctor: DoctorRunner,
        context_trust: ContextInspector,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
    ) -> None:
        self._doctor = doctor
        self._context_trust = context_trust
        self._unit_of_work = unit_of_work

    def cached(self) -> OnboardingState:
        with self._unit_of_work() as uow:
            state = uow.guided_learning.get_state()
        report = (
            EnvironmentReadinessReport.model_validate(state.last_environment_report)
            if state.last_environment_report is not None
            else None
        )
        return OnboardingState(
            first_use=state.onboarding_completed_at is None,
            completed_at=state.onboarding_completed_at,
            report=report,
        )

    def check(self, requirements: LabRequirements | None = None) -> EnvironmentReadinessReport:
        doctor_report = self._doctor.run()
        checks = [self._public_check(check) for check in doctor_report.checks]
        checks.append(self._context_check())
        if requirements is not None:
            checks.extend(self._requirement_checks(doctor_report, requirements))
        status = _aggregate_readiness(checks)
        report = EnvironmentReadinessReport(
            status=status,
            checks=tuple(checks),
            generated_at=doctor_report.generated_at,
        )
        with self._unit_of_work() as uow:
            uow.guided_learning.save_environment_report(
                status=status.value,
                report=report.model_dump(mode="json"),
                checked_at=report.generated_at,
            )
            uow.commit()
        return report

    def assert_ready(self, requirements: LabRequirements) -> EnvironmentReadinessReport:
        report = self.check(requirements)
        if report.status is ReadinessStatus.BLOCKED:
            raise EnvironmentNotReadyError(report)
        return report

    @staticmethod
    def _public_check(check: DiagnosticCheck) -> ReadinessCheck:
        check_id = check.id
        status = ReadinessCheckStatus(check.status.value)
        message = _safe_text(check.message)
        remediation = None
        commands: tuple[str, ...] = ()
        if status in {ReadinessCheckStatus.FAIL, ReadinessCheckStatus.WARN}:
            fixed = _FIXED_REMEDIATION.get(check_id)
            if fixed is not None:
                remediation, commands = fixed
        return ReadinessCheck(
            id=check_id,
            status=status,
            message=message,
            remediation=remediation,
            commands=commands,
        )

    def _context_check(self) -> ReadinessCheck:
        try:
            inspection = self._context_trust.inspect()
        except Exception:
            return _fixed_check(
                "context_trust",
                ReadinessCheckStatus.FAIL,
                "无法安全检查当前Context信任状态。",
            )
        if inspection.trust_state is TrustState.TRUSTED:
            return ReadinessCheck(
                id="context_trust",
                status=ReadinessCheckStatus.PASS,
                message="当前本地minikube Context已受信任。",
            )
        message = (
            "当前Context信任指纹已漂移。"
            if inspection.trust_state is TrustState.DRIFTED
            else "当前本地minikube Context尚未受信任。"
        )
        return _fixed_check("context_trust", ReadinessCheckStatus.FAIL, message)

    @staticmethod
    def _requirement_checks(
        report: DoctorReport, requirements: LabRequirements
    ) -> tuple[ReadinessCheck, ...]:
        source = {check.id: check for check in report.checks}
        checks: list[ReadinessCheck] = []
        resources = source.get("cluster_resources")
        cpu_value = resources.details.get("cpu_cores", 0) if resources else 0
        memory_value = resources.details.get("memory_mib", 0) if resources else 0
        cpu = float(cpu_value) if isinstance(cpu_value, (int, float)) else 0
        memory = int(memory_value) if isinstance(memory_value, (int, float)) else 0
        resources_ok = cpu >= requirements.minimum_cpu and memory >= requirements.minimum_memory_mib
        checks.append(
            _fixed_check(
                "lab_resources",
                ReadinessCheckStatus.PASS if resources_ok else ReadinessCheckStatus.FAIL,
                ("集群资源满足当前实验要求。" if resources_ok else "集群资源低于当前实验要求。"),
                fallback="cluster_resources",
            )
        )
        api = source.get("cluster_api")
        version = str(api.details.get("version", "")) if api else ""
        version_ok = _version_satisfies(version, requirements.kubernetes)
        checks.append(
            _fixed_check(
                "lab_kubernetes_version",
                ReadinessCheckStatus.PASS if version_ok else ReadinessCheckStatus.FAIL,
                message=(
                    "Kubernetes版本满足当前实验要求。"
                    if version_ok
                    else "Kubernetes版本不满足当前实验要求。"
                ),
            )
        )
        addon_ids = {"ingress": "ingress_addon", "metrics-server": "metrics_server_addon"}
        for addon in requirements.addons:
            source_check = source.get(addon_ids.get(addon, ""))
            available = source_check is not None and source_check.status is CheckStatus.PASS
            checks.append(
                _fixed_check(
                    f"lab_addon_{addon}",
                    ReadinessCheckStatus.PASS if available else ReadinessCheckStatus.FAIL,
                    f"实验所需Addon {addon}{'已就绪' if available else '不可用'}。",
                    fallback=addon_ids.get(addon),
                )
            )
        return tuple(checks)


def _fixed_check(
    check_id: str,
    status: ReadinessCheckStatus,
    message: str,
    *,
    fallback: str | None = None,
) -> ReadinessCheck:
    remediation = None
    commands: tuple[str, ...] = ()
    fixed = _FIXED_REMEDIATION.get(fallback or check_id)
    if status is not ReadinessCheckStatus.PASS and fixed is not None:
        remediation, commands = fixed
    return ReadinessCheck(
        id=check_id,
        status=status,
        message=message,
        remediation=remediation,
        commands=commands,
    )


def _aggregate_readiness(checks: list[ReadinessCheck]) -> ReadinessStatus:
    if any(check.status is ReadinessCheckStatus.FAIL for check in checks):
        return ReadinessStatus.BLOCKED
    if any(check.status is ReadinessCheckStatus.WARN for check in checks):
        return ReadinessStatus.DEGRADED
    return ReadinessStatus.READY


def _version_satisfies(actual: str, requirement: str) -> bool:
    actual_match = re.search(r"v?(\d+)\.(\d+)", actual)
    requirement_match = re.fullmatch(r">=\s*(\d+)\.(\d+)", requirement.strip())
    if actual_match is None or requirement_match is None:
        return False
    actual_version = int(actual_match.group(1)), int(actual_match.group(2))
    minimum = int(requirement_match.group(1)), int(requirement_match.group(2))
    return actual_version >= minimum


def _safe_text(value: str) -> str:
    safe = str(redact_json(value.replace("\r", " ").replace("\n", " ")))[:500]
    lowered = safe.casefold()
    if "traceback" in lowered or ("apiversion:" in lowered and "kind:" in lowered):
        return "检查详情不可公开。"
    return safe


def public_validation_outcome(status: ValidationStatus) -> PublicValidationOutcome:
    if status is ValidationStatus.PASSED:
        return PublicValidationOutcome.PASSED
    if status is ValidationStatus.FAILED:
        return PublicValidationOutcome.FAILED
    return PublicValidationOutcome.UNAVAILABLE


def build_environment_readiness_service(
    *,
    doctor: DoctorService,
    context_trust: ContextInspector,
    unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
) -> EnvironmentReadinessService:
    return EnvironmentReadinessService(
        doctor=doctor,
        context_trust=context_trust,
        unit_of_work=unit_of_work,
    )


__all__ = [
    "EnvironmentNotReadyError",
    "EnvironmentReadinessReport",
    "EnvironmentReadinessService",
    "OnboardingState",
    "PublicValidationOutcome",
    "ReadinessCheck",
    "ReadinessCheckStatus",
    "ReadinessStatus",
    "build_environment_readiness_service",
    "public_validation_outcome",
]
