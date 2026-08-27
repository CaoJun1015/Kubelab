"""Restricted workspace kubeconfig and cleanup tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from kubelab.kubernetes_gateway import WorkspaceAccess
from kubelab.workspace import WorkspaceError, workspace_environment, write_workspace_kubeconfig


def access() -> WorkspaceAccess:
    return WorkspaceAccess(
        session_id="123e4567-e89b-42d3-a456-426614174000",
        namespace="kubelab-complete-lab",
        token=SecretStr("short-lived-token"),
    )


def write_source(path: Path, *, ca: str = "Q0E=") -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "minikube",
                "clusters": [
                    {
                        "name": "minikube",
                        "cluster": {
                            "server": "https://127.0.0.1:8443",
                            "certificate-authority-data": ca,
                        },
                    }
                ],
                "contexts": [
                    {
                        "name": "minikube",
                        "context": {"cluster": "minikube", "user": "admin"},
                    }
                ],
                "users": [
                    {
                        "name": "admin",
                        "user": {"client-key-data": "PRIVATE", "token": "ADMIN"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_workspace_kubeconfig_contains_only_ephemeral_credentials(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "workspace" / "config"
    write_source(source)

    write_workspace_kubeconfig(source, destination, access())

    text = destination.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    assert payload["current-context"] == "kubelab-workspace"
    assert payload["contexts"][0]["context"]["namespace"] == "kubelab-complete-lab"
    assert payload["users"][0]["user"] == {"token": "short-lived-token"}
    assert "PRIVATE" not in text
    assert "ADMIN" not in text
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_workspace_kubeconfig_embeds_relative_ca_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    ca = tmp_path / "minikube-ca.crt"
    ca.write_bytes(b"local-ca")
    write_source(source)
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    cluster = payload["clusters"][0]["cluster"]
    cluster.pop("certificate-authority-data")
    cluster["certificate-authority"] = ca.name
    source.write_text(yaml.safe_dump(payload), encoding="utf-8")

    destination = tmp_path / "workspace-config"
    write_workspace_kubeconfig(source, destination, access())

    result = yaml.safe_load(destination.read_text(encoding="utf-8"))
    assert result["clusters"][0]["cluster"]["certificate-authority-data"] == "bG9jYWwtY2E="


@pytest.mark.parametrize("payload", ["not: [valid", "[]", "{}"])
def test_workspace_rejects_invalid_source_without_leaking_credentials(
    tmp_path: Path, payload: str
) -> None:
    source = tmp_path / "source"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(WorkspaceError) as error:
        write_workspace_kubeconfig(source, tmp_path / "destination", access())

    assert "short-lived-token" not in str(error.value)


class FakeManager:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def open_workspace(self) -> WorkspaceAccess:
        return access()

    def close_workspace(self, session_id: str) -> None:
        self.closed.append(session_id)


def test_workspace_environment_removes_file_and_revokes_on_error(tmp_path: Path) -> None:
    manager = FakeManager()
    source = tmp_path / "source"
    write_source(source)
    generated: Path | None = None

    with pytest.raises(RuntimeError):
        with workspace_environment(  # type: ignore[arg-type]
            manager, source, temporary_root=tmp_path
        ) as environment:
            generated = environment.kubeconfig_path
            assert generated.exists()
            raise RuntimeError("shell failed")

    assert generated is not None and not generated.exists()
    assert manager.closed == [access().session_id]
