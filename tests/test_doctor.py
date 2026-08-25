from pathlib import Path

import pytest

from kubelab.config import KubeLabConfig, KubernetesSettings, ToolName
from kubelab.doctor import (
    CheckStatus,
    ClusterInspectionError,
    ClusterSnapshot,
    DoctorService,
    HealthStatus,
    NodeSnapshot,
    RuntimeSnapshot,
    inspect_runtime,
)
from kubelab.tools import CommandResult, LocatedTool, ToolSource


class FakeLocator:
    def __init__(self, available: set[ToolName]) -> None:
        self.available = available

    def locate(self, name: ToolName) -> LocatedTool | None:
        if name not in self.available:
            return None
        return LocatedTool(name=name, path=Path(f"/usr/bin/{name.value}"), source=ToolSource.PATH)


class FakeRunner:
    def __init__(
        self,
        *,
        docker_ok: bool = True,
        minikube_ok: bool = True,
        kubectl_version: str = "v1.35.1",
    ) -> None:
        self.docker_ok = docker_ok
        self.minikube_ok = minikube_ok
        self.kubectl_version = kubectl_version

    def run(
        self, executable: Path, arguments: list[str], *, timeout_seconds: int = 10
    ) -> CommandResult:
        del timeout_seconds
        if executable.stem == "docker" and arguments[0] == "info":
            return CommandResult(
                args=(str(executable), *arguments),
                returncode=0 if self.docker_ok else 1,
                stdout="27.0.0" if self.docker_ok else "",
                stderr="" if self.docker_ok else "daemon unavailable",
            )
        if executable.stem == "minikube" and arguments[0] == "status":
            return CommandResult(
                args=(str(executable), *arguments),
                returncode=0 if self.minikube_ok else 7,
                stdout=(
                    '{"Host":"Running","Kubelet":"Running","APIServer":"Running"}'
                    if self.minikube_ok
                    else ""
                ),
                stderr="" if self.minikube_ok else "Stopped",
            )
        if executable.stem == "kubectl":
            return CommandResult(
                args=(str(executable), *arguments),
                returncode=0,
                stdout=(
                    '{"clientVersion":{"gitVersion":'
                    f'"{self.kubectl_version}"'  # Values are controlled by unit tests.
                    "}}"
                ),
                stderr="",
            )
        if executable.stem == "minikube":
            return CommandResult(
                args=(str(executable), *arguments),
                returncode=0,
                stdout='{"minikubeVersion":"v1.38.1"}',
                stderr="",
            )
        return CommandResult(
            args=(str(executable), *arguments), returncode=0, stdout="v1", stderr=""
        )


class FakeInspector:
    def __init__(
        self, snapshot: ClusterSnapshot | None = None, error: ClusterInspectionError | None = None
    ) -> None:
        self.snapshot = snapshot or healthy_snapshot()
        self.error = error
        self.called = False

    def inspect(self, kubeconfig_path: Path) -> ClusterSnapshot:
        self.called = True
        if self.error is not None:
            raise self.error
        assert kubeconfig_path.exists()
        return self.snapshot


def healthy_snapshot() -> ClusterSnapshot:
    return ClusterSnapshot(
        context_name="minikube",
        server="https://127.0.0.1:54321",
        kubernetes_version="v1.35.1",
        nodes=[NodeSnapshot(name="minikube", ready=True, cpu_cores=4, memory_mib=6144)],
        default_storage_class="standard",
        ingress_enabled=True,
        metrics_server_enabled=True,
    )


def healthy_runtime() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        os_name="Linux",
        kernel_release="6.6.87.2-microsoft-standard-WSL2",
        is_wsl2=True,
        distro_id="ubuntu",
        distro_name="Ubuntu",
        distro_version="24.04",
    )


def make_service(
    tmp_path: Path,
    *,
    available: set[ToolName] | None = None,
    runner: FakeRunner | None = None,
    inspector: FakeInspector | None = None,
    runtime: RuntimeSnapshot | None = None,
) -> tuple[DoctorService, Path, FakeInspector]:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("test", encoding="utf-8")
    config = KubeLabConfig(kubernetes=KubernetesSettings(kubeconfig=kubeconfig))
    resolved_inspector = inspector or FakeInspector()
    service = DoctorService(
        config=config,
        locator=FakeLocator(available or set(ToolName)),
        runner=runner or FakeRunner(),
        cluster_inspector=resolved_inspector,
        python_version=(3, 11, 0),
        runtime=runtime or healthy_runtime(),
    )
    return service, kubeconfig, resolved_inspector


def check(report, check_id: str):
    return next(item for item in report.checks if item.id == check_id)


def test_healthy_environment_passes_all_required_checks(tmp_path: Path) -> None:
    """A complete local minikube environment should report healthy."""
    service, _, _ = make_service(tmp_path)

    report = service.run()

    assert report.status is HealthStatus.HEALTHY
    assert check(report, "runtime_platform").status is CheckStatus.PASS
    assert check(report, "ubuntu_distribution").status is CheckStatus.PASS
    assert check(report, "nodes_ready").status is CheckStatus.PASS
    assert check(report, "cluster_resources").status is CheckStatus.PASS
    assert check(report, "kubectl_cli").details["version"] == "v1.35.1"
    assert check(report, "kubectl_version_skew").status is CheckStatus.PASS
    assert check(report, "minikube_cli").details["version"] == "v1.38.1"


def test_missing_docker_fails_and_skips_daemon_check(tmp_path: Path) -> None:
    """A missing Docker CLI must not be followed by a fabricated daemon check."""
    available = set(ToolName) - {ToolName.DOCKER}
    service, _, _ = make_service(tmp_path, available=available)

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "docker_cli").status is CheckStatus.FAIL
    assert check(report, "docker_daemon").status is CheckStatus.SKIPPED


def test_stopped_docker_daemon_is_unhealthy(tmp_path: Path) -> None:
    """Finding the docker CLI is insufficient when the daemon is unavailable."""
    service, _, _ = make_service(tmp_path, runner=FakeRunner(docker_ok=False))

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "docker_daemon").status is CheckStatus.FAIL


def test_stopped_minikube_skips_cluster_inspection(tmp_path: Path) -> None:
    """Cluster checks must be skipped when minikube is stopped."""
    inspector = FakeInspector()
    service, _, resolved_inspector = make_service(
        tmp_path, runner=FakeRunner(minikube_ok=False), inspector=inspector
    )

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "minikube_status").status is CheckStatus.FAIL
    assert check(report, "kubeconfig").status is CheckStatus.SKIPPED
    assert resolved_inspector.called is False


def test_missing_kubeconfig_is_reported_before_api_call(tmp_path: Path) -> None:
    """A missing kubeconfig must not trigger a Kubernetes API attempt."""
    inspector = FakeInspector()
    service, kubeconfig, resolved_inspector = make_service(tmp_path, inspector=inspector)
    kubeconfig.unlink()

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "kubeconfig").status is CheckStatus.FAIL
    assert check(report, "cluster_api").status is CheckStatus.SKIPPED
    assert resolved_inspector.called is False


def test_cluster_api_error_skips_dependent_checks(tmp_path: Path) -> None:
    """API failures must be distinguished from node and addon states."""
    inspector = FakeInspector(error=ClusterInspectionError("authentication failed"))
    service, _, _ = make_service(tmp_path, inspector=inspector)

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "cluster_api").status is CheckStatus.FAIL
    assert check(report, "kubectl_version_skew").status is CheckStatus.SKIPPED
    assert check(report, "nodes_ready").status is CheckStatus.SKIPPED
    assert "authentication failed" in check(report, "cluster_api").message


@pytest.mark.parametrize(
    ("client_version", "server_version", "expected_status", "minor_difference"),
    [
        ("v1.35.1", "v1.35.1", CheckStatus.PASS, 0),
        ("v1.34.9", "v1.35.1", CheckStatus.PASS, 1),
        ("v1.36.0", "v1.35.1", CheckStatus.PASS, 1),
        ("v1.33.7", "v1.35.1", CheckStatus.FAIL, 2),
        ("v1.37.0", "v1.35.1", CheckStatus.FAIL, 2),
    ],
)
def test_kubectl_version_skew_uses_one_minor_limit(
    tmp_path: Path,
    client_version: str,
    server_version: str,
    expected_status: CheckStatus,
    minor_difference: int,
) -> None:
    """kubectl may be at most one minor newer or older than the API Server."""
    snapshot = healthy_snapshot()
    snapshot.kubernetes_version = server_version
    service, _, _ = make_service(
        tmp_path,
        runner=FakeRunner(kubectl_version=client_version),
        inspector=FakeInspector(snapshot=snapshot),
    )

    report = service.run()

    skew = check(report, "kubectl_version_skew")
    assert skew.status is expected_status
    assert skew.details["client_version"] == client_version
    assert skew.details["server_version"] == server_version
    assert skew.details["minor_difference"] == minor_difference


@pytest.mark.parametrize(
    ("client_version", "server_version"),
    [("v2.0.0", "v1.35.1"), ("unknown", "v1.35.1"), ("v1.35.1", "unknown")],
)
def test_kubectl_version_skew_rejects_major_mismatch_or_invalid_version(
    tmp_path: Path, client_version: str, server_version: str
) -> None:
    """Uncomparable versions must fail instead of implying compatibility."""
    snapshot = healthy_snapshot()
    snapshot.kubernetes_version = server_version
    service, _, _ = make_service(
        tmp_path,
        runner=FakeRunner(kubectl_version=client_version),
        inspector=FakeInspector(snapshot=snapshot),
    )

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "kubectl_version_skew").status is CheckStatus.FAIL


def test_not_ready_node_and_insufficient_resources_fail(tmp_path: Path) -> None:
    """A reachable cluster can still be unsuitable for KubeLab experiments."""
    snapshot = healthy_snapshot()
    snapshot.nodes = [NodeSnapshot(name="minikube", ready=False, cpu_cores=1, memory_mib=1024)]
    service, _, _ = make_service(tmp_path, inspector=FakeInspector(snapshot=snapshot))

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "nodes_ready").status is CheckStatus.FAIL
    assert check(report, "cluster_resources").status is CheckStatus.FAIL


def test_missing_optional_tools_and_addons_are_degraded(tmp_path: Path) -> None:
    """Optional tooling and addons should warn without making the environment unhealthy."""
    snapshot = healthy_snapshot()
    snapshot.ingress_enabled = False
    snapshot.metrics_server_enabled = False
    available = set(ToolName) - {ToolName.HELM}
    service, _, _ = make_service(
        tmp_path, available=available, inspector=FakeInspector(snapshot=snapshot)
    )

    report = service.run()

    assert report.status is HealthStatus.DEGRADED
    assert check(report, "helm_cli").status is CheckStatus.WARN
    assert check(report, "ingress_addon").status is CheckStatus.WARN
    assert check(report, "metrics_server_addon").status is CheckStatus.WARN


def test_windows_runtime_is_rejected(tmp_path: Path) -> None:
    """Running the KubeLab process on Windows must fail the formal platform baseline."""
    runtime = RuntimeSnapshot(os_name="Windows", kernel_release="10.0.26100", is_wsl2=False)
    service, _, _ = make_service(tmp_path, runtime=runtime)

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "runtime_platform").status is CheckStatus.FAIL
    assert check(report, "ubuntu_distribution").status is CheckStatus.SKIPPED


def test_non_ubuntu_wsl2_runtime_is_rejected(tmp_path: Path) -> None:
    """General Linux and non-Ubuntu WSL distributions are outside the MVP baseline."""
    runtime = healthy_runtime()
    runtime.distro_id = "debian"
    runtime.distro_name = "Debian GNU/Linux"
    service, _, _ = make_service(tmp_path, runtime=runtime)

    report = service.run()

    assert report.status is HealthStatus.UNHEALTHY
    assert check(report, "runtime_platform").status is CheckStatus.PASS
    assert check(report, "ubuntu_distribution").status is CheckStatus.FAIL


def test_runtime_inspection_reads_wsl2_ubuntu_markers(tmp_path: Path) -> None:
    """Runtime inspection should use kernel and os-release facts without a subprocess."""
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nNAME="Ubuntu"\nVERSION_ID="24.04"\n', encoding="utf-8")

    runtime = inspect_runtime(
        os_release,
        os_name="Linux",
        kernel_release="6.6.87.2-microsoft-standard-WSL2",
    )

    assert runtime.is_wsl2 is True
    assert runtime.distro_id == "ubuntu"
    assert runtime.distro_version == "24.04"
