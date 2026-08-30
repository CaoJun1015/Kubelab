import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kubelab import cli
from kubelab.cli import app
from kubelab.config import ConfigError, TrustedContext
from kubelab.context_trust import (
    ContextInspection,
    ContextNotLocalMinikubeError,
    TrustState,
)
from kubelab.doctor import CheckStatus, DiagnosticCheck, DoctorReport, HealthStatus
from kubelab.lab_manager import PracticeMode
from kubelab.session_state import LabSessionSnapshot, SessionStatus
from kubelab.workspace import WorkspaceEnvironment

runner = CliRunner()


def test_public_session_json_hides_internal_variant_and_context() -> None:
    session = LabSessionSnapshot(
        id="123e4567-e89b-42d3-a456-426614174111",
        lab_id="lab-013-service-target-port",
        variant_id="variant-b",
        namespace="kubelab-service-target-port",
        status=SessionStatus.IN_PROGRESS,
        context_name="minikube",
        context_fingerprint="a" * 64,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
        completed_at=None,
        reset_count=0,
        last_error_code=None,
        last_error_context={"token": "must-not-leak"},
    )

    payload = cli._public_session_payload(
        session,
        practice_mode=PracticeMode.BLIND_REPEAT,
        scenario_revealed=False,
    )

    assert payload["practice_mode"] == "blind_repeat"
    assert payload["scenario_revealed"] is False
    assert "variant_id" not in payload
    assert "context_fingerprint" not in payload
    assert "last_error_context" not in payload


def test_help_shows_product_and_usage() -> None:
    """The package baseline must expose a discoverable command-line entry point."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "KubeLab" in result.stdout
    assert "Usage" in result.stdout
    assert "doctor" in result.stdout
    assert "config" in result.stdout
    assert "context" in result.stdout
    assert "serve" in result.stdout
    assert "workspace" in result.stdout


def test_workspace_enter_uses_fixed_bash_and_ephemeral_environment(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[list[str], bool, dict[str, str]]] = []

    class Runtime:
        manager = object()
        kubeconfig_path = tmp_path / "source"

        def close(self) -> None:
            return None

    @contextmanager
    def fake_environment(manager, source) -> Iterator[WorkspaceEnvironment]:
        del manager, source
        yield WorkspaceEnvironment(
            session_id="123e4567-e89b-42d3-a456-426614174000",
            namespace="kubelab-complete-lab",
            kubeconfig_path=tmp_path / "restricted-config",
        )

    def fake_run(argv, *, check, env):
        calls.append((argv, check, env))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli, "build_application_runtime", Runtime)
    monkeypatch.setattr(cli, "workspace_environment", fake_environment)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = runner.invoke(app, ["workspace", "enter"])

    assert result.exit_code == 0
    assert calls[0][0] == ["/bin/bash", "--noprofile", "--norc", "-i"]
    assert calls[0][1] is False
    assert calls[0][2]["KUBECONFIG"] == str(tmp_path / "restricted-config")
    assert calls[0][2]["KUBELAB_NAMESPACE"] == "kubelab-complete-lab"


def test_serve_uses_only_the_fixed_loopback_endpoint(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    application = object()
    monkeypatch.setattr(cli, "create_app", lambda: application)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, *, host, port: calls.append((app, host, port)),
    )

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert calls == [(application, "127.0.0.1", 8765)]


def test_version_reports_package_version() -> None:
    """The CLI version must stay aligned with installed package metadata."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "KubeLab 0.3.0rc1"


def test_doctor_json_uses_stable_public_shape(monkeypatch) -> None:
    """JSON diagnostics must be machine-readable and must not expose internal data."""
    report = DoctorReport(
        status=HealthStatus.HEALTHY,
        checks=[
            DiagnosticCheck(
                id="python",
                status=CheckStatus.PASS,
                message="Python 3.11 is active.",
                details={"version": "3.11.0"},
            )
        ],
    )

    class FakeDoctorService:
        def run(self) -> DoctorReport:
            return report

    monkeypatch.setattr(cli, "build_doctor_service", lambda: FakeDoctorService())

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "healthy"
    assert payload["checks"][0]["id"] == "python"
    assert "traceback" not in result.stdout.lower()


def test_unhealthy_doctor_exits_with_environment_code(monkeypatch) -> None:
    """An unhealthy local environment must be reported with exit code three."""
    report = DoctorReport(
        status=HealthStatus.UNHEALTHY,
        checks=[
            DiagnosticCheck(
                id="docker_cli",
                status=CheckStatus.FAIL,
                message="Docker was not found.",
                remediation="Configure its absolute path.",
            )
        ],
    )

    class FakeDoctorService:
        def run(self) -> DoctorReport:
            return report

    monkeypatch.setattr(cli, "build_doctor_service", lambda: FakeDoctorService())

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.stdout)["status"] == "unhealthy"


def test_doctor_text_renders_status_and_remediation(monkeypatch) -> None:
    """Human output should remain compact while retaining the next repair action."""
    report = DoctorReport(
        status=HealthStatus.DEGRADED,
        checks=[
            DiagnosticCheck(
                id="helm_cli",
                status=CheckStatus.WARN,
                message="Helm was not found.",
                remediation="Install Helm if a lab needs it.",
            )
        ],
    )

    class FakeDoctorService:
        def run(self) -> DoctorReport:
            return report

    monkeypatch.setattr(cli, "build_doctor_service", lambda: FakeDoctorService())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "KubeLab environment: degraded" in result.stdout
    assert "[WARN] helm_cli" in result.stdout
    assert "Fix: Install Helm" in result.stdout


def test_doctor_reports_configuration_error_without_traceback(monkeypatch) -> None:
    """Invalid local configuration is a usage error, not an internal crash."""

    def fail_to_build() -> None:
        raise ConfigError("invalid TOML")

    monkeypatch.setattr(cli, "build_doctor_service", fail_to_build)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 2
    assert "Configuration error: invalid TOML" in result.stderr
    assert "traceback" not in result.stderr.lower()


def test_config_set_tool_writes_explicit_path(tmp_path: Path, monkeypatch) -> None:
    """The public configuration command should persist a validated executable."""
    config_path = tmp_path / "config.toml"
    executable = tmp_path / "kubelab-tools" / "docker"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setattr(cli, "get_config_path", lambda: config_path)

    result = runner.invoke(app, ["config", "set-tool", "docker", str(executable)])

    assert result.exit_code == 0
    assert "Configured docker" in result.stdout
    assert config_path.exists()


def test_config_set_tool_rejects_invalid_path(tmp_path: Path, monkeypatch) -> None:
    """Configuration validation failures should use exit code two."""
    monkeypatch.setattr(cli, "get_config_path", lambda: tmp_path / "config.toml")

    result = runner.invoke(app, ["config", "set-tool", "docker", "docker"])

    assert result.exit_code == 2
    assert "must be absolute" in result.stderr


def context_inspection() -> ContextInspection:
    return ContextInspection(
        context_name="minikube",
        minikube_profile="minikube",
        api_server="https://127.0.0.1:32771",
        ca_sha256="a" * 64,
        kube_system_uid="uid-kube-system",
        kubernetes_version="v1.35.1",
        trusted=True,
        trust_state=TrustState.TRUSTED,
    )


def context_record() -> TrustedContext:
    return TrustedContext(
        name="minikube",
        server="https://127.0.0.1:32771",
        ca_sha256="a" * 64,
        kube_system_uid="uid-kube-system",
        minikube_profile="minikube",
        trusted_at=datetime(2026, 8, 25, 16, 0, tzinfo=UTC),
    )


class FakeContextService:
    def inspect(self) -> ContextInspection:
        return context_inspection()

    def trust(self) -> TrustedContext:
        return context_record()

    def untrust(self) -> tuple[str, bool]:
        return "minikube", True


def test_context_inspect_supports_human_and_credential_free_json_output(monkeypatch) -> None:
    monkeypatch.setattr(cli, "build_context_trust_service", FakeContextService)

    human = runner.invoke(app, ["context", "inspect"])
    machine = runner.invoke(app, ["context", "inspect", "--json"])

    assert human.exit_code == 0
    assert "Context: minikube" in human.stdout
    assert "Kubernetes Server: v1.35.1" in human.stdout
    assert "Trusted: yes" in human.stdout
    payload = json.loads(machine.stdout)
    assert payload["trust_state"] == "trusted"
    assert payload["ca_sha256"] == "a" * 64
    assert "token" not in machine.stdout.lower()
    assert "certificate-authority-data" not in machine.stdout.lower()


def test_context_trust_and_untrust_render_safe_identity(monkeypatch) -> None:
    monkeypatch.setattr(cli, "build_context_trust_service", FakeContextService)

    trust = runner.invoke(app, ["context", "trust"])
    untrust = runner.invoke(app, ["context", "untrust"])

    assert trust.exit_code == 0
    assert "Trusted context: minikube" in trust.stdout
    assert "CA SHA256" in trust.stdout
    assert untrust.exit_code == 0
    assert "Removed trust for context: minikube" in untrust.stdout


def test_context_untrust_is_idempotent_in_cli(monkeypatch) -> None:
    service = FakeContextService()
    service.untrust = lambda: ("minikube", False)  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "build_context_trust_service", lambda: service)

    result = runner.invoke(app, ["context", "untrust"])

    assert result.exit_code == 0
    assert "Context was not trusted" in result.stdout


def test_context_errors_use_exit_three_without_traceback(monkeypatch) -> None:
    service = FakeContextService()

    def reject_trust() -> TrustedContext:
        raise ContextNotLocalMinikubeError("remote context rejected; token=<redacted>")

    service.trust = reject_trust  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "build_context_trust_service", lambda: service)

    result = runner.invoke(app, ["context", "trust"])

    assert result.exit_code == 3
    assert "CONTEXT_NOT_LOCAL_MINIKUBE" in result.stderr
    assert "traceback" not in result.stderr.lower()


def test_context_configuration_errors_use_exit_two(monkeypatch) -> None:
    def fail_to_build():
        raise ConfigError("invalid local config")

    monkeypatch.setattr(cli, "build_context_trust_service", fail_to_build)

    result = runner.invoke(app, ["context", "inspect"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
