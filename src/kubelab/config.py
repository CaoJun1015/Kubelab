"""KubeLab local configuration loading and atomic persistence."""

from __future__ import annotations

import os
import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError


class ConfigError(RuntimeError):
    """Base error for invalid or unavailable KubeLab configuration."""


class ConfigWriteError(ConfigError):
    """Raised when a configuration update cannot be committed atomically."""


class ToolPathError(ConfigError):
    """Raised when a configured executable path is unsafe or invalid."""


class ToolName(StrEnum):
    """External tools understood by the MVP environment doctor."""

    DOCKER = "docker"
    KUBECTL = "kubectl"
    MINIKUBE = "minikube"
    HELM = "helm"


class ToolsConfig(BaseModel):
    """Explicit executable path overrides."""

    model_config = ConfigDict(extra="forbid")

    docker: Path | None = None
    kubectl: Path | None = None
    minikube: Path | None = None
    helm: Path | None = None

    def path_for(self, name: ToolName) -> Path | None:
        """Return the configured path for one supported tool."""
        paths = {
            ToolName.DOCKER: self.docker,
            ToolName.KUBECTL: self.kubectl,
            ToolName.MINIKUBE: self.minikube,
            ToolName.HELM: self.helm,
        }
        return paths[name]


class KubernetesSettings(BaseModel):
    """Read-only Kubernetes client settings used by doctor."""

    model_config = ConfigDict(extra="forbid")

    kubeconfig: Path | None = None


class TrustedContext(BaseModel):
    """Persisted identity of one explicitly trusted local minikube context."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    server: str = Field(min_length=1)
    ca_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kube_system_uid: str = Field(min_length=1)
    minikube_profile: str = Field(min_length=1)
    trusted_at: AwareDatetime


class KubeLabConfig(BaseModel):
    """Top-level local configuration."""

    model_config = ConfigDict(extra="forbid")

    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    trusted_contexts: list[TrustedContext] = Field(default_factory=list)


def get_config_path() -> Path:
    """Resolve the XDG configuration file without creating it."""
    override = os.environ.get("KUBELAB_CONFIG_FILE")
    if override:
        return _require_wsl_native_path(Path(override).expanduser(), "configuration file")
    xdg_config_home = _absolute_xdg_path("XDG_CONFIG_HOME")
    base = xdg_config_home or Path.home() / ".config"
    return _require_wsl_native_path(base / "kubelab" / "config.toml", "configuration file")


def get_data_dir() -> Path:
    """Resolve the WSL-local state directory for databases, locks, and logs."""
    override = os.environ.get("KUBELAB_DATA_DIR")
    if override:
        return _require_wsl_native_path(Path(override).expanduser(), "state directory")
    xdg_state_home = _absolute_xdg_path("XDG_STATE_HOME")
    path = (xdg_state_home or Path.home() / ".local" / "state") / "kubelab"
    return _require_wsl_native_path(path, "state directory")


def load_config(path: Path | None = None) -> KubeLabConfig:
    """Load configuration, returning safe defaults when the file is absent."""
    config_path = path or get_config_path()
    if not config_path.exists():
        return KubeLabConfig()
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
        return KubeLabConfig.model_validate(raw)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ConfigError(f"Failed to load configuration from {config_path}: {exc}") from exc


def save_config(config: KubeLabConfig, path: Path | None = None) -> Path:
    """Persist configuration using a same-directory atomic replacement."""
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = tomli_w.dumps(_toml_data(config)).encode("utf-8")
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, config_path)
    except OSError as exc:
        raise ConfigWriteError(f"Failed to write configuration to {config_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return config_path


def set_tool_path(config_path: Path, tool: ToolName, executable: Path) -> KubeLabConfig:
    """Validate and atomically store an explicit executable path."""
    if not executable.is_absolute():
        raise ToolPathError(f"Tool path must be absolute: {executable}")
    if not executable.exists():
        raise ToolPathError(f"Tool path does not exist: {executable}")
    if not executable.is_file():
        raise ToolPathError(f"Tool path is not a file: {executable}")

    config = load_config(config_path)
    config.tools = config.tools.model_copy(update={tool.value: executable.resolve()})
    save_config(config, config_path)
    return config


def resolve_kubeconfig_path(config: KubeLabConfig) -> Path:
    """Resolve kubeconfig precedence without reading any credential content."""
    if config.kubernetes.kubeconfig is not None:
        return config.kubernetes.kubeconfig.expanduser()
    override = os.environ.get("KUBELAB_KUBECONFIG")
    if override:
        return Path(override).expanduser()
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig:
        return Path(kubeconfig.split(os.pathsep, maxsplit=1)[0]).expanduser()
    return Path.home() / ".kube" / "config"


def _toml_data(config: KubeLabConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json", exclude_none=True)
    return dict(data)


def _absolute_xdg_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else None


def _require_wsl_native_path(path: Path, purpose: str) -> Path:
    normalized = path.as_posix().lower()
    parts = normalized.split("/")
    if len(parts) >= 4 and parts[1] == "mnt" and len(parts[2]) == 1:
        raise ConfigError(f"KubeLab {purpose} must use the WSL Linux filesystem, not DrvFs: {path}")
    return path
