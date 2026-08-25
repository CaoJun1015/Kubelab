import subprocess
from pathlib import Path

import pytest

from kubelab.config import ToolName, ToolsConfig
from kubelab.tools import (
    ProcessRunner,
    ProcessTimeoutError,
    ToolExecutionError,
    ToolLocator,
    ToolSource,
    default_linux_locations,
)


def test_tool_locator_prefers_explicit_configuration(tmp_path: Path) -> None:
    """An explicit path must win over PATH and Linux fallback locations."""
    configured = tmp_path / "configured-tools" / "docker"
    configured.parent.mkdir()
    configured.touch()
    path_tool = tmp_path / "path-docker"
    path_tool.touch()
    locator = ToolLocator(
        ToolsConfig(docker=configured),
        which=lambda _: str(path_tool),
        known_locations={ToolName.DOCKER: ()},
    )

    result = locator.locate(ToolName.DOCKER)

    assert result is not None
    assert result.path == configured.resolve()
    assert result.source is ToolSource.CONFIG


def test_tool_locator_falls_back_to_path(tmp_path: Path) -> None:
    """PATH discovery is used when no explicit configuration exists."""
    executable = tmp_path / "kubectl"
    executable.touch()
    locator = ToolLocator(
        ToolsConfig(),
        which=lambda name: str(executable) if name == "kubectl" else None,
        known_locations={},
    )

    result = locator.locate(ToolName.KUBECTL)

    assert result is not None
    assert result.path == executable.resolve()
    assert result.source is ToolSource.PATH


def test_tool_locator_returns_none_when_tool_is_absent() -> None:
    """Missing tools are diagnostic results, not guessed paths."""
    locator = ToolLocator(ToolsConfig(), which=lambda _: None, known_locations={})

    assert locator.locate(ToolName.HELM) is None


def test_tool_locator_uses_existing_known_location(tmp_path: Path) -> None:
    """Linux fallback discovery is used only after configuration and PATH."""
    executable = tmp_path / "helm"
    executable.touch()
    locator = ToolLocator(
        ToolsConfig(),
        which=lambda _: None,
        known_locations={ToolName.HELM: (executable,)},
    )

    result = locator.locate(ToolName.HELM)

    assert result is not None
    assert result.source is ToolSource.KNOWN_LOCATION


def test_default_linux_locations_include_user_and_system_prefixes(tmp_path: Path) -> None:
    """WSL tools installed system-wide or in ~/.local/bin must be discoverable."""
    locations = default_linux_locations(tmp_path)

    assert Path("/usr/local/bin/docker") in locations[ToolName.DOCKER]
    assert tmp_path / ".local" / "bin" / "kubectl" in locations[ToolName.KUBECTL]


def test_process_runner_preserves_nonzero_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor must be able to interpret a tool's nonzero exit and stderr."""
    executable = tmp_path / "tool"
    executable.touch()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=7, stdout="", stderr="stopped")

    monkeypatch.setattr("kubelab.tools.subprocess.run", fake_run)

    result = ProcessRunner().run(executable, ["status"])

    assert result.returncode == 7
    assert result.stderr == "stopped"


def test_process_runner_converts_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External command timeouts must include executable and duration context."""
    executable = tmp_path / "tool"
    executable.touch()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr("kubelab.tools.subprocess.run", fake_run)

    with pytest.raises(ProcessTimeoutError, match="timed out after 2 seconds"):
        ProcessRunner().run(executable, ["status"], timeout_seconds=2)


def test_process_runner_converts_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Process startup errors must not leak implementation tracebacks."""
    executable = tmp_path / "tool"
    executable.touch()

    def fake_run(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr("kubelab.tools.subprocess.run", fake_run)

    with pytest.raises(ToolExecutionError, match="Failed to execute"):
        ProcessRunner().run(executable, ["version"])


def test_process_runner_truncates_large_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostic output must be bounded before it reaches JSON or logs."""
    executable = tmp_path / "tool"
    executable.touch()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="abcdef", stderr="")

    monkeypatch.setattr("kubelab.tools.subprocess.run", fake_run)

    result = ProcessRunner(max_output_bytes=3).run(executable, ["version"])

    assert result.truncated is True
    assert result.stdout == "abc\n[output truncated]"
