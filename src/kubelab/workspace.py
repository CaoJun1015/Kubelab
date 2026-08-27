"""Ephemeral kubeconfig construction for the restricted WSL workspace shell."""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kubelab.kubernetes_gateway import WorkspaceAccess
from kubelab.lab_manager import LabManager


class WorkspaceError(RuntimeError):
    """Sanitized failure while preparing the local restricted workspace."""

    code = "WORKSPACE_SETUP_FAILED"
    retryable = False


@dataclass(frozen=True)
class WorkspaceEnvironment:
    session_id: str
    namespace: str
    kubeconfig_path: Path


@contextmanager
def workspace_environment(
    manager: LabManager,
    source_kubeconfig: Path,
    *,
    temporary_root: Path = Path("/tmp"),
) -> Iterator[WorkspaceEnvironment]:
    """Provision credentials and guarantee best-effort revocation on shell exit."""
    access = manager.open_workspace()
    try:
        with tempfile.TemporaryDirectory(prefix="kubelab-workspace-", dir=temporary_root) as value:
            kubeconfig_path = Path(value) / "config"
            write_workspace_kubeconfig(source_kubeconfig, kubeconfig_path, access)
            yield WorkspaceEnvironment(
                session_id=access.session_id,
                namespace=access.namespace,
                kubeconfig_path=kubeconfig_path,
            )
    finally:
        manager.close_workspace(access.session_id)


def write_workspace_kubeconfig(
    source_path: Path,
    destination: Path,
    access: WorkspaceAccess,
) -> None:
    """Write a credential-minimal kubeconfig with mode 0600."""
    source = _load_kubeconfig(source_path)
    cluster = _current_cluster(source, source_path)
    payload = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "kubelab", "cluster": cluster}],
        "contexts": [
            {
                "name": "kubelab-workspace",
                "context": {
                    "cluster": "kubelab",
                    "user": "kubelab-workspace",
                    "namespace": access.namespace,
                },
            }
        ],
        "current-context": "kubelab-workspace",
        "users": [
            {
                "name": "kubelab-workspace",
                "user": {"token": access.token.get_secret_value()},
            }
        ],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(payload, stream, sort_keys=False)
        destination.chmod(0o600)
    except OSError as exc:
        raise WorkspaceError("KubeLab could not write the temporary workspace kubeconfig.") from exc


def _load_kubeconfig(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise WorkspaceError("KubeLab could not read the configured kubeconfig.") from exc
    if not isinstance(value, Mapping):
        raise WorkspaceError("The configured kubeconfig is invalid.")
    return value


def _current_cluster(source: Mapping[str, Any], source_path: Path) -> dict[str, Any]:
    context_name = source.get("current-context")
    contexts = source.get("contexts")
    clusters = source.get("clusters")
    if not isinstance(context_name, str) or not isinstance(contexts, list):
        raise WorkspaceError("The configured kubeconfig has no current Context.")
    context = _named_entry(contexts, context_name, "Context")
    context_value = context.get("context")
    if not isinstance(context_value, Mapping) or not isinstance(context_value.get("cluster"), str):
        raise WorkspaceError("The configured kubeconfig Context has no cluster.")
    if not isinstance(clusters, list):
        raise WorkspaceError("The configured kubeconfig has no clusters.")
    entry = _named_entry(clusters, str(context_value["cluster"]), "cluster")
    value = entry.get("cluster")
    if not isinstance(value, Mapping) or not isinstance(value.get("server"), str):
        raise WorkspaceError("The configured kubeconfig cluster has no API server.")
    result: dict[str, Any] = {"server": value["server"]}
    ca_data = value.get("certificate-authority-data")
    if isinstance(ca_data, str) and ca_data:
        result["certificate-authority-data"] = ca_data
        return result
    ca_path = value.get("certificate-authority")
    if isinstance(ca_path, str) and ca_path:
        candidate = Path(ca_path).expanduser()
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        try:
            result["certificate-authority-data"] = base64.b64encode(candidate.read_bytes()).decode(
                "ascii"
            )
        except OSError as exc:
            raise WorkspaceError("KubeLab could not read the Kubernetes CA certificate.") from exc
        return result
    raise WorkspaceError("The configured kubeconfig cluster has no CA certificate.")


def _named_entry(entries: list[Any], name: str, kind: str) -> Mapping[str, Any]:
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("name") == name:
            return entry
    raise WorkspaceError(f"The configured kubeconfig {kind} was not found.")


__all__ = [
    "WorkspaceEnvironment",
    "WorkspaceError",
    "workspace_environment",
    "write_workspace_kubeconfig",
]
