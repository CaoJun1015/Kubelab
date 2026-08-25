import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kubelab.config import (
    ConfigError,
    ConfigWriteError,
    KubeLabConfig,
    ToolName,
    ToolPathError,
    TrustedContext,
    get_config_path,
    get_data_dir,
    load_config,
    resolve_kubeconfig_path,
    save_config,
    set_tool_path,
)


def test_set_tool_path_round_trips_linux_path_with_spaces(tmp_path: Path) -> None:
    """Configured Linux tool paths must survive TOML serialization unchanged."""
    config_path = tmp_path / "config.toml"
    executable = tmp_path / "KubeLab Tools" / "docker"
    executable.parent.mkdir(parents=True)
    executable.touch()

    set_tool_path(config_path, ToolName.DOCKER, executable)

    assert load_config(config_path).tools.docker == executable.resolve()


@pytest.mark.parametrize("candidate", [Path("docker"), Path("tools/docker")])
def test_set_tool_path_rejects_relative_path(tmp_path: Path, candidate: Path) -> None:
    """Relative paths are ambiguous and must never reach ProcessRunner."""
    with pytest.raises(ToolPathError, match="absolute"):
        set_tool_path(tmp_path / "config.toml", ToolName.DOCKER, candidate)


def test_set_tool_path_rejects_missing_file(tmp_path: Path) -> None:
    """A configured tool path must identify an existing file."""
    with pytest.raises(ToolPathError, match="does not exist"):
        set_tool_path(tmp_path / "config.toml", ToolName.KUBECTL, tmp_path / "kubectl")


def test_atomic_write_failure_preserves_previous_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed replacement must not corrupt the last known-good configuration."""
    config_path = tmp_path / "config.toml"
    first = tmp_path / "docker"
    second = tmp_path / "docker-new"
    first.touch()
    second.touch()
    set_tool_path(config_path, ToolName.DOCKER, first)

    def fail_replace(source: Path, destination: Path) -> None:
        raise PermissionError("replacement denied")

    monkeypatch.setattr("kubelab.config.os.replace", fail_replace)

    with pytest.raises(ConfigWriteError, match="Failed to write"):
        set_tool_path(config_path, ToolName.DOCKER, second)

    assert load_config(config_path).tools.docker == first.resolve()


def test_config_path_honors_override_and_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config location must follow the XDG convention and remain overridable."""
    explicit = tmp_path / "custom.toml"
    monkeypatch.setenv("KUBELAB_CONFIG_FILE", str(explicit))
    assert get_config_path() == explicit

    monkeypatch.delenv("KUBELAB_CONFIG_FILE")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert get_config_path() == tmp_path / "kubelab" / "config.toml"


def test_data_dir_honors_override_and_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Database, lock, and log state must remain on the WSL Linux filesystem."""
    explicit = tmp_path / "explicit-state"
    monkeypatch.setenv("KUBELAB_DATA_DIR", str(explicit))
    assert get_data_dir() == explicit

    monkeypatch.delenv("KUBELAB_DATA_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert get_data_dir() == tmp_path / "kubelab"


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("KUBELAB_CONFIG_FILE", "/mnt/d/kubelab/config.toml"),
        ("KUBELAB_DATA_DIR", "/mnt/c/kubelab-state"),
    ],
)
def test_runtime_state_rejects_drvfs_paths(
    environment_name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration and mutable state must not use Windows-mounted DrvFs paths."""
    monkeypatch.setenv(environment_name, value)

    with pytest.raises(ConfigError, match="WSL Linux filesystem"):
        get_config_path() if environment_name == "KUBELAB_CONFIG_FILE" else get_data_dir()


def test_load_config_rejects_invalid_toml(tmp_path: Path) -> None:
    """Malformed local configuration must become a stable domain error."""
    config_path = tmp_path / "config.toml"
    config_path.write_text("[tools\ndocker =", encoding="utf-8")

    with pytest.raises(ConfigError, match="Failed to load"):
        load_config(config_path)


def test_set_tool_path_rejects_directory(tmp_path: Path) -> None:
    """A directory cannot be executed as a configured tool."""
    with pytest.raises(ToolPathError, match="not a file"):
        set_tool_path(tmp_path / "config.toml", ToolName.HELM, tmp_path)


def test_kubeconfig_resolution_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit config, KubeLab override, then standard KUBECONFIG must win in order."""
    configured = tmp_path / "configured"
    config = KubeLabConfig()
    config.kubernetes.kubeconfig = configured
    assert resolve_kubeconfig_path(config) == configured

    override = tmp_path / "override"
    monkeypatch.setenv("KUBELAB_KUBECONFIG", str(override))
    assert resolve_kubeconfig_path(KubeLabConfig()) == override

    monkeypatch.delenv("KUBELAB_KUBECONFIG")
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("KUBECONFIG", f"{first}{os.pathsep}{second}")
    assert resolve_kubeconfig_path(KubeLabConfig()) == first


def test_trusted_context_round_trips_as_credential_free_toml(tmp_path: Path) -> None:
    """Trust records persist only hashes and stable credential-free identity facts."""
    config_path = tmp_path / "config.toml"
    record = TrustedContext(
        name="minikube",
        server="https://127.0.0.1:32771",
        ca_sha256="a" * 64,
        kube_system_uid="uid-kube-system",
        minikube_profile="minikube",
        trusted_at=datetime(2026, 8, 25, 16, 0, tzinfo=UTC),
    )

    save_config(KubeLabConfig(trusted_contexts=[record]), config_path)

    assert load_config(config_path).trusted_contexts == [record]
    serialized = config_path.read_text(encoding="utf-8").lower()
    assert "token" not in serialized
    assert "client-key" not in serialized
