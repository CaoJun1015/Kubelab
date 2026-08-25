"""Read-only diagnostics for the local KubeLab runtime environment."""

from __future__ import annotations

import json
import platform
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.utils.quantity import parse_quantity
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from kubelab.config import KubeLabConfig, ToolName, load_config, resolve_kubeconfig_path
from kubelab.tools import (
    CommandResult,
    LocatedTool,
    ProcessRunner,
    ToolExecutionError,
    ToolLocator,
)

MIN_CPU_CORES = 2.0
MIN_MEMORY_MIB = 2048
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(token|authorization|client-key-data|client-certificate-data)\s*[:=]\s*\S+"
)


class CheckStatus(StrEnum):
    """Outcome of one environment check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class HealthStatus(StrEnum):
    """Aggregate environment readiness."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class DiagnosticCheck(BaseModel):
    """Stable public result for one doctor check."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: CheckStatus
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    remediation: str | None = None


class DoctorReport(BaseModel):
    """Machine-readable diagnostic report."""

    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    checks: list[DiagnosticCheck]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NodeSnapshot(BaseModel):
    """Only the non-sensitive node capacity needed by doctor."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ready: bool
    cpu_cores: float
    memory_mib: int


class ClusterSnapshot(BaseModel):
    """Sanitized cluster facts gathered through read-only API calls."""

    model_config = ConfigDict(extra="forbid")

    context_name: str
    server: str
    kubernetes_version: str
    nodes: list[NodeSnapshot]
    default_storage_class: str | None
    ingress_enabled: bool
    metrics_server_enabled: bool


class RuntimeSnapshot(BaseModel):
    """Non-sensitive host facts used to enforce the supported runtime."""

    model_config = ConfigDict(extra="forbid")

    os_name: str
    kernel_release: str
    is_wsl2: bool
    distro_id: str | None = None
    distro_name: str | None = None
    distro_version: str | None = None


class ClusterInspectionError(RuntimeError):
    """Raised when kubeconfig or the Kubernetes API cannot be inspected safely."""


class Locator(Protocol):
    """Structural interface used to isolate tool discovery in tests."""

    def locate(self, name: ToolName) -> LocatedTool | None: ...


class Runner(Protocol):
    """Structural interface used to isolate external processes in tests."""

    def run(
        self,
        executable: Path,
        arguments: Sequence[str],
        *,
        timeout_seconds: int = 10,
    ) -> CommandResult: ...


class ClusterInspector(Protocol):
    """Structural interface for read-only cluster inspection."""

    def inspect(self, kubeconfig_path: Path) -> ClusterSnapshot: ...


class KubernetesClusterInspector:
    """Collect a bounded, read-only snapshot through the official client."""

    def inspect(self, kubeconfig_path: Path) -> ClusterSnapshot:  # pragma: no cover
        try:
            contexts, current = kube_config.list_kube_config_contexts(
                config_file=str(kubeconfig_path)
            )
            if not contexts or not current:
                raise ClusterInspectionError("kubeconfig has no current context")
            context_name = str(current.get("name", ""))

            kube_config.load_kube_config(config_file=str(kubeconfig_path), context=context_name)
            server = _safe_server(client.Configuration.get_default_copy().host)
            version = client.VersionApi().get_code().git_version or "unknown"
            nodes = [self._node_snapshot(item) for item in client.CoreV1Api().list_node().items]
            storage_classes = client.StorageV1Api().list_storage_class().items
            default_storage = next(
                (
                    item.metadata.name
                    for item in storage_classes
                    if (item.metadata.annotations or {}).get(
                        "storageclass.kubernetes.io/is-default-class"
                    )
                    == "true"
                ),
                None,
            )
            apps = client.AppsV1Api()
            return ClusterSnapshot(
                context_name=context_name,
                server=server,
                kubernetes_version=version,
                nodes=nodes,
                default_storage_class=default_storage,
                ingress_enabled=self._deployment_available(
                    apps, "ingress-nginx", "ingress-nginx-controller"
                ),
                metrics_server_enabled=self._deployment_available(
                    apps, "kube-system", "metrics-server"
                ),
            )
        except ClusterInspectionError:
            raise
        except Exception as exc:
            raise ClusterInspectionError(_safe_message(exc)) from exc

    @staticmethod
    def _node_snapshot(node: client.V1Node) -> NodeSnapshot:  # pragma: no cover
        metadata = node.metadata
        status = node.status
        conditions = status.conditions or []
        ready = any(
            condition.type == "Ready" and condition.status == "True" for condition in conditions
        )
        allocatable = status.allocatable or {}
        cpu = float(Decimal(parse_quantity(allocatable.get("cpu", "0"))))
        memory_bytes = Decimal(parse_quantity(allocatable.get("memory", "0")))
        return NodeSnapshot(
            name=metadata.name or "unknown",
            ready=ready,
            cpu_cores=cpu,
            memory_mib=int(memory_bytes / Decimal(1024**2)),
        )

    @staticmethod
    def _deployment_available(
        apps: client.AppsV1Api, namespace: str, name: str
    ) -> bool:  # pragma: no cover
        try:
            deployment = apps.read_namespaced_deployment_status(name, namespace)
            return (deployment.status.available_replicas or 0) > 0
        except client.ApiException as exc:
            if exc.status == 404:
                return False
            raise


class DoctorService:
    """Orchestrate dependency-aware, read-only environment checks."""

    def __init__(
        self,
        *,
        config: KubeLabConfig,
        locator: Locator,
        runner: Runner,
        cluster_inspector: ClusterInspector,
        python_version: tuple[int, int, int],
        runtime: RuntimeSnapshot,
    ) -> None:
        self._config = config
        self._locator = locator
        self._runner = runner
        self._cluster_inspector = cluster_inspector
        self._python_version = python_version
        self._runtime = runtime

    def run(self) -> DoctorReport:
        """Execute checks in dependency order and aggregate their health."""
        checks = [*self._check_runtime(), self._check_python()]
        _, docker_cli = self._check_tool(ToolName.DOCKER, required=True)
        checks.append(docker_cli)
        checks.append(self._check_docker_daemon(docker_cli))

        kubectl, kubectl_cli = self._check_tool(ToolName.KUBECTL, required=True)
        checks.append(kubectl_cli)
        minikube, minikube_cli = self._check_tool(ToolName.MINIKUBE, required=True)
        checks.append(minikube_cli)
        minikube_status = self._check_minikube_status(minikube, minikube_cli)
        checks.append(minikube_status)
        _, helm_cli = self._check_tool(ToolName.HELM, required=False)
        checks.append(helm_cli)

        cluster_prerequisites = (
            kubectl is not None
            and kubectl_cli.status is CheckStatus.PASS
            and minikube_status.status is CheckStatus.PASS
        )
        kubectl_version = str(kubectl_cli.details.get("version", "unknown"))
        checks.extend(self._check_cluster(cluster_prerequisites, kubectl_version))
        return DoctorReport(status=self._health(checks), checks=checks)

    def _check_runtime(self) -> list[DiagnosticCheck]:
        if not self._runtime.is_wsl2:
            return [
                _check(
                    "runtime_platform",
                    CheckStatus.FAIL,
                    "KubeLab must run inside WSL2 Ubuntu.",
                    remediation="Open the Ubuntu WSL2 terminal and run KubeLab there.",
                    os=self._runtime.os_name,
                    kernel=self._runtime.kernel_release,
                ),
                _skipped("ubuntu_distribution", "the current process is not running in WSL2."),
            ]

        platform_check = _check(
            "runtime_platform",
            CheckStatus.PASS,
            "KubeLab is running inside WSL2.",
            os=self._runtime.os_name,
            kernel=self._runtime.kernel_release,
        )
        if self._runtime.distro_id != "ubuntu":
            return [
                platform_check,
                _check(
                    "ubuntu_distribution",
                    CheckStatus.FAIL,
                    "The WSL2 distribution is not Ubuntu.",
                    remediation="Run KubeLab in an Ubuntu WSL2 distribution.",
                    id=self._runtime.distro_id or "unknown",
                    name=self._runtime.distro_name or "unknown",
                    version=self._runtime.distro_version or "unknown",
                ),
            ]
        return [
            platform_check,
            _check(
                "ubuntu_distribution",
                CheckStatus.PASS,
                "The WSL2 distribution is Ubuntu.",
                id=self._runtime.distro_id or "unknown",
                name=self._runtime.distro_name or "unknown",
                version=self._runtime.distro_version or "unknown",
            ),
        ]

    def _check_python(self) -> DiagnosticCheck:
        version = ".".join(str(part) for part in self._python_version)
        if self._python_version[:2] == (3, 11):
            return _check("python", CheckStatus.PASS, "Python 3.11 is active.", version=version)
        return _check(
            "python",
            CheckStatus.FAIL,
            f"Python {version} is active; KubeLab requires Python 3.11.",
            remediation="Install Python 3.11 and recreate the uv environment.",
            version=version,
        )

    def _check_tool(
        self, name: ToolName, *, required: bool
    ) -> tuple[LocatedTool | None, DiagnosticCheck]:
        tool = self._locator.locate(name)
        status = CheckStatus.FAIL if required else CheckStatus.WARN
        if tool is None:
            return None, _check(
                f"{name.value}_cli",
                status,
                f"{name.value} CLI was not found.",
                remediation=(
                    f"Install {name.value} or run 'kubelab config set-tool {name.value} "
                    "<absolute-path>'."
                ),
            )
        version_arguments = {
            ToolName.DOCKER: ["--version"],
            ToolName.KUBECTL: ["version", "--client=true", "--output=json"],
            ToolName.MINIKUBE: ["version", "--output=json"],
            ToolName.HELM: ["version", "--short"],
        }
        try:
            result = self._runner.run(tool.path, version_arguments[name])
        except ToolExecutionError as exc:
            return tool, _check(
                f"{name.value}_cli",
                status,
                f"{name.value} CLI could not be executed: {_safe_message(exc)}",
                remediation="Verify the configured executable and local permissions.",
                source=tool.source.value,
                path=str(tool.path),
            )
        if result.returncode != 0:
            return tool, _check(
                f"{name.value}_cli",
                status,
                f"{name.value} CLI returned exit code {result.returncode}.",
                remediation="Run the version command manually and repair the installation.",
                source=tool.source.value,
                path=str(tool.path),
            )
        version = _extract_tool_version(name, result.stdout)
        return tool, _check(
            f"{name.value}_cli",
            CheckStatus.PASS,
            f"{name.value} CLI is available.",
            source=tool.source.value,
            path=str(tool.path),
            version=version,
        )

    def _check_docker_daemon(self, cli_check: DiagnosticCheck) -> DiagnosticCheck:
        if cli_check.status is not CheckStatus.PASS:
            return _skipped("docker_daemon", "Docker CLI is unavailable.")
        tool = self._locator.locate(ToolName.DOCKER)
        if tool is None:
            return _skipped("docker_daemon", "Docker CLI is unavailable.")
        try:
            result = self._runner.run(tool.path, ["info", "--format", "{{.ServerVersion}}"])
        except ToolExecutionError as exc:
            return _check(
                "docker_daemon",
                CheckStatus.FAIL,
                f"Docker daemon check failed: {_safe_message(exc)}",
                remediation="Start Docker Engine inside WSL2 Ubuntu and verify 'docker info'.",
            )
        if result.returncode != 0:
            return _check(
                "docker_daemon",
                CheckStatus.FAIL,
                "Docker CLI is present, but the daemon is unavailable.",
                remediation="Start Docker Engine inside WSL2 Ubuntu and verify 'docker info'.",
                exit_code=result.returncode,
            )
        return _check("docker_daemon", CheckStatus.PASS, "Docker daemon is reachable.")

    def _check_minikube_status(
        self, tool: LocatedTool | None, cli_check: DiagnosticCheck
    ) -> DiagnosticCheck:
        if tool is None or cli_check.status is not CheckStatus.PASS:
            return _skipped("minikube_status", "minikube CLI is unavailable.")
        try:
            result = self._runner.run(tool.path, ["status", "--output=json"])
        except ToolExecutionError as exc:
            return _check(
                "minikube_status",
                CheckStatus.FAIL,
                f"minikube status failed: {_safe_message(exc)}",
                remediation="Start the local cluster with 'minikube start'.",
            )
        if result.returncode != 0:
            return _check(
                "minikube_status",
                CheckStatus.FAIL,
                "The minikube profile is not running.",
                remediation="Run 'minikube start' and repeat the doctor check.",
                exit_code=result.returncode,
            )
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            return _check(
                "minikube_status",
                CheckStatus.FAIL,
                "minikube returned an invalid status response.",
                remediation="Run 'minikube status' manually and inspect the profile.",
            )
        components = {
            name: str(status.get(name, "Unknown")) for name in ("Host", "Kubelet", "APIServer")
        }
        if any(value.lower() != "running" for value in components.values()):
            return _check(
                "minikube_status",
                CheckStatus.FAIL,
                "One or more minikube components are not running.",
                remediation="Run 'minikube start' and wait for all components.",
                **components,
            )
        return _check(
            "minikube_status", CheckStatus.PASS, "The minikube profile is running.", **components
        )

    def _check_cluster(
        self, prerequisites_met: bool, kubectl_version: str
    ) -> list[DiagnosticCheck]:
        dependent_ids = (
            "kubeconfig",
            "current_context",
            "cluster_api",
            "kubectl_version_skew",
            "nodes_ready",
            "cluster_resources",
            "default_storage_class",
            "ingress_addon",
            "metrics_server_addon",
        )
        if not prerequisites_met:
            return [
                _skipped(check_id, "kubectl or minikube is unavailable.")
                for check_id in dependent_ids
            ]

        kubeconfig_path = resolve_kubeconfig_path(self._config)
        if not kubeconfig_path.is_file():
            return [
                _check(
                    "kubeconfig",
                    CheckStatus.FAIL,
                    f"kubeconfig was not found at {kubeconfig_path}.",
                    remediation="Start minikube or configure a valid local kubeconfig path.",
                ),
                *[
                    _skipped(check_id, "kubeconfig is unavailable.")
                    for check_id in dependent_ids[1:]
                ],
            ]

        checks = [
            _check(
                "kubeconfig",
                CheckStatus.PASS,
                "kubeconfig is available.",
                path=str(kubeconfig_path),
            )
        ]
        try:
            snapshot = self._cluster_inspector.inspect(kubeconfig_path)
        except ClusterInspectionError as exc:
            checks.extend(
                [
                    _skipped("current_context", "Kubernetes API inspection failed."),
                    _check(
                        "cluster_api",
                        CheckStatus.FAIL,
                        f"Kubernetes API is unreachable: {_safe_message(exc)}",
                        remediation="Verify the current context and run 'minikube status'.",
                    ),
                    *[
                        _skipped(check_id, "Kubernetes API is unreachable.")
                        for check_id in dependent_ids[3:]
                    ],
                ]
            )
            return checks

        checks.extend(self._snapshot_checks(snapshot, kubectl_version))
        return checks

    @staticmethod
    def _snapshot_checks(snapshot: ClusterSnapshot, kubectl_version: str) -> list[DiagnosticCheck]:
        context = _check(
            "current_context",
            CheckStatus.PASS,
            f"Current context is {snapshot.context_name}.",
            context=snapshot.context_name,
            server=snapshot.server,
        )
        api = _check(
            "cluster_api",
            CheckStatus.PASS,
            "Kubernetes API is reachable.",
            version=snapshot.kubernetes_version,
        )
        version_skew = _kubectl_version_skew_check(kubectl_version, snapshot.kubernetes_version)
        not_ready = [node.name for node in snapshot.nodes if not node.ready]
        nodes = (
            _check(
                "nodes_ready",
                CheckStatus.FAIL,
                "One or more Kubernetes nodes are not Ready.",
                remediation="Inspect 'kubectl get nodes' and node conditions.",
                not_ready=cast(JsonValue, not_ready),
            )
            if not snapshot.nodes or not_ready
            else _check(
                "nodes_ready",
                CheckStatus.PASS,
                "All Kubernetes nodes are Ready.",
                count=len(snapshot.nodes),
            )
        )
        cpu = sum(node.cpu_cores for node in snapshot.nodes)
        memory = sum(node.memory_mib for node in snapshot.nodes)
        resources_ok = cpu >= MIN_CPU_CORES and memory >= MIN_MEMORY_MIB
        resources = _check(
            "cluster_resources",
            CheckStatus.PASS if resources_ok else CheckStatus.FAIL,
            (
                "Cluster allocatable resources meet the MVP minimum."
                if resources_ok
                else "Cluster allocatable resources are below the MVP minimum."
            ),
            remediation=(
                None
                if resources_ok
                else "Increase minikube to at least 2 CPUs and 2048 MiB of memory."
            ),
            cpu_cores=cpu,
            memory_mib=memory,
            minimum_cpu_cores=MIN_CPU_CORES,
            minimum_memory_mib=MIN_MEMORY_MIB,
        )
        storage = (
            _check(
                "default_storage_class",
                CheckStatus.PASS,
                f"Default StorageClass is {snapshot.default_storage_class}.",
                name=snapshot.default_storage_class,
            )
            if snapshot.default_storage_class
            else _check(
                "default_storage_class",
                CheckStatus.WARN,
                "No default StorageClass was found.",
                remediation="Enable the minikube default-storageclass addon.",
            )
        )
        ingress = _addon_check("ingress_addon", "Ingress", snapshot.ingress_enabled)
        metrics = _addon_check(
            "metrics_server_addon", "metrics-server", snapshot.metrics_server_enabled
        )
        return [context, api, version_skew, nodes, resources, storage, ingress, metrics]

    @staticmethod
    def _health(checks: Sequence[DiagnosticCheck]) -> HealthStatus:
        if any(check.status is CheckStatus.FAIL for check in checks):
            return HealthStatus.UNHEALTHY
        if any(check.status is CheckStatus.WARN for check in checks):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY


def build_doctor_service() -> DoctorService:  # pragma: no cover - composition root
    """Build the production doctor service from local configuration."""
    local_config = load_config()
    return DoctorService(
        config=local_config,
        locator=ToolLocator(local_config.tools),
        runner=ProcessRunner(),
        cluster_inspector=KubernetesClusterInspector(),
        python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        runtime=inspect_runtime(),
    )


def inspect_runtime(
    os_release_path: Path = Path("/etc/os-release"),
    *,
    os_name: str | None = None,
    kernel_release: str | None = None,
) -> RuntimeSnapshot:
    """Inspect WSL2 and Ubuntu markers without invoking a shell command."""
    resolved_os_name = os_name or platform.system()
    resolved_kernel = kernel_release or platform.release()
    normalized_kernel = resolved_kernel.lower()
    is_wsl2 = (
        resolved_os_name == "Linux"
        and "microsoft" in normalized_kernel
        and "wsl2" in normalized_kernel
    )
    release = _read_os_release(os_release_path) if resolved_os_name == "Linux" else {}
    return RuntimeSnapshot(
        os_name=resolved_os_name,
        kernel_release=resolved_kernel,
        is_wsl2=is_wsl2,
        distro_id=release.get("ID"),
        distro_name=release.get("NAME"),
        distro_version=release.get("VERSION_ID"),
    )


def _check(
    check_id: str,
    status: CheckStatus,
    message: str,
    *,
    remediation: str | None = None,
    **details: JsonValue,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        id=check_id,
        status=status,
        message=message,
        details=details,
        remediation=remediation,
    )


def _skipped(check_id: str, reason: str) -> DiagnosticCheck:
    return _check(check_id, CheckStatus.SKIPPED, f"Skipped: {reason}")


def _addon_check(check_id: str, label: str, enabled: bool) -> DiagnosticCheck:
    if enabled:
        return _check(check_id, CheckStatus.PASS, f"{label} is available.")
    return _check(
        check_id,
        CheckStatus.WARN,
        f"{label} is not available.",
        remediation=f"Enable the minikube {label.lower()} addon if the lab needs it.",
    )


def _kubectl_version_skew_check(kubectl_version: str, server_version: str) -> DiagnosticCheck:
    client = _parse_kubernetes_major_minor(kubectl_version)
    server = _parse_kubernetes_major_minor(server_version)
    if client is None or server is None:
        return _check(
            "kubectl_version_skew",
            CheckStatus.FAIL,
            "kubectl and Kubernetes Server versions could not be compared.",
            remediation="Install a stable kubectl version within one minor of the API Server.",
            client_version=kubectl_version,
            server_version=server_version,
        )

    if client[0] != server[0]:
        return _check(
            "kubectl_version_skew",
            CheckStatus.FAIL,
            "kubectl and Kubernetes Server major versions do not match.",
            remediation="Install kubectl with the same major and within one minor of the server.",
            client_version=kubectl_version,
            server_version=server_version,
        )

    minor_difference = abs(client[1] - server[1])
    if minor_difference > 1:
        return _check(
            "kubectl_version_skew",
            CheckStatus.FAIL,
            f"kubectl differs from the Kubernetes Server by {minor_difference} minor versions.",
            remediation="Install kubectl within one minor of the Kubernetes API Server.",
            client_version=kubectl_version,
            server_version=server_version,
            minor_difference=minor_difference,
        )
    return _check(
        "kubectl_version_skew",
        CheckStatus.PASS,
        "kubectl is within one minor of the Kubernetes Server.",
        client_version=kubectl_version,
        server_version=server_version,
        minor_difference=minor_difference,
    )


def _parse_kubernetes_major_minor(version: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+)?(?:[-+].*)?", version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _safe_message(error: BaseException) -> str:
    return _safe_text(str(error)) or error.__class__.__name__


def _safe_text(value: str) -> str:
    message = value.replace("\r", " ").replace("\n", " ")[:500]
    return _SENSITIVE_PATTERN.sub(r"\1=<redacted>", message)


def _safe_server(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return "configured"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _extract_tool_version(name: ToolName, output: str) -> str:
    stripped = output.strip()
    if not stripped:
        return "available"
    if name in {ToolName.KUBECTL, ToolName.MINIKUBE}:
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if name is ToolName.KUBECTL:
                client_version = payload.get("clientVersion")
                if isinstance(client_version, dict):
                    git_version = client_version.get("gitVersion")
                    if isinstance(git_version, str):
                        return _safe_text(git_version)
            minikube_version = payload.get("minikubeVersion")
            if isinstance(minikube_version, str):
                return _safe_text(minikube_version)
    return _safe_text(stripped.splitlines()[0])


def _read_os_release(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    release: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        release[key] = value.strip().strip('"').strip("'")
    return release
