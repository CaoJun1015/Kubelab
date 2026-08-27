"""Isolation, path-safety, and manifest-policy tests for LabRegistry."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from kubelab.lab_registry import (
    LabMaterializationError,
    LabRegistry,
    RegistryErrorCode,
    _portable_relative_path,
)


def lab_data(lab_id: str, manifest: str = "manifest.yaml") -> dict[str, Any]:
    return {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "Lab",
        "metadata": {
            "id": lab_id,
            "name": f"Lab {lab_id}",
            "description": "Registry test lab.",
            "difficulty": "beginner",
            "durationMinutes": 10,
            "category": "testing",
            "tags": ["pod"],
        },
        "requirements": {
            "kubernetes": ">=1.28",
            "minimumCpu": 1,
            "minimumMemoryMiB": 512,
            "addons": [],
        },
        "environment": {
            "namespace": f"kubelab-{lab_id}",
            "manifests": [manifest],
            "provisionTimeoutSeconds": 30,
        },
        "task": {
            "description": "Find the fault.",
            "completionDescription": "Fix the fault.",
            "successMessage": "Fixed.",
        },
        "initialChecks": [
            {
                "id": "pod-running",
                "type": "pod_status",
                "selector": {"app": lab_id},
                "expectedPhase": "Running",
                "minimumCount": 1,
                "timeoutSeconds": 10,
                "unmetMessage": "Pod is not running.",
            }
        ],
        "successChecks": [
            {
                "id": "pod-exists",
                "type": "resource_exists",
                "apiVersion": "v1",
                "kind": "Pod",
                "name": lab_id,
                "timeoutSeconds": 10,
                "unmetMessage": "Pod is missing.",
            }
        ],
        "hints": [{"level": 1, "content": "Inspect the Pod."}],
        "cleanup": {"deleteNamespace": True},
        "interview": {"questions": ["What was wrong?"]},
    }


def pod_manifest(lab_id: str, pod_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": lab_id},
        "spec": pod_spec or {"containers": [{"name": "web", "image": "nginx:1.27"}]},
    }


def write_lab(
    root: Path,
    directory: str,
    data: dict[str, Any],
    manifest: str | dict[str, Any] | list[dict[str, Any]] | None,
) -> Path:
    lab_dir = root / directory
    lab_dir.mkdir(parents=True)
    (lab_dir / "lab.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n"
    )
    if manifest is not None:
        manifest_path = lab_dir / data["environment"]["manifests"][0]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(manifest, str):
            payload = manifest
        elif isinstance(manifest, list):
            payload = yaml.safe_dump_all(manifest, sort_keys=False)
        else:
            payload = yaml.safe_dump(manifest, sort_keys=False)
        manifest_path.write_text(payload, encoding="utf-8", newline="\n")
    return lab_dir


def codes(snapshot: Any) -> set[RegistryErrorCode]:
    return {error.code for error in snapshot.errors}


def test_committed_valid_fixture_loads_with_multidocument_manifest() -> None:
    fixture = Path(__file__).parent / "fixtures" / "labs" / "valid"

    snapshot = LabRegistry(fixture).scan()

    assert [lab.definition.metadata.id for lab in snapshot.labs] == ["complete-lab"]
    assert snapshot.errors == ()
    assert len(snapshot.labs[0].manifest_sha256[0]) == 64


def test_committed_invalid_fixtures_are_isolated_and_redacted() -> None:
    fixture = Path(__file__).parent / "fixtures" / "labs" / "invalid"

    snapshot = LabRegistry(fixture).scan()

    assert snapshot.labs == ()
    assert RegistryErrorCode.LAB_YAML_INVALID in codes(snapshot)
    assert RegistryErrorCode.MANIFEST_UNSAFE in codes(snapshot)
    assert all("privileged: true" not in error.message for error in snapshot.errors)


def test_bad_lab_does_not_block_good_lab_and_order_is_deterministic(tmp_path: Path) -> None:
    write_lab(tmp_path, "z-good", lab_data("z-good"), pod_manifest("z-good"))
    bad = write_lab(tmp_path, "a-bad", lab_data("a-bad"), pod_manifest("a-bad"))
    (bad / "lab.yaml").write_text("metadata: [broken", encoding="utf-8")
    write_lab(tmp_path, "m-good", lab_data("m-good"), pod_manifest("m-good"))

    first = LabRegistry(tmp_path).scan()
    second = LabRegistry(tmp_path).scan()

    assert [lab.lab_path for lab in first.labs] == ["m-good/lab.yaml", "z-good/lab.yaml"]
    assert first == second
    assert codes(first) == {RegistryErrorCode.LAB_YAML_INVALID}


def test_duplicate_ids_reject_every_conflicting_lab(tmp_path: Path) -> None:
    write_lab(tmp_path, "one", lab_data("duplicate-lab"), pod_manifest("duplicate-lab"))
    write_lab(tmp_path, "two", lab_data("duplicate-lab"), pod_manifest("duplicate-lab"))

    snapshot = LabRegistry(tmp_path).scan()

    assert snapshot.labs == ()
    duplicate_errors = [
        error for error in snapshot.errors if error.code is RegistryErrorCode.LAB_DUPLICATE_ID
    ]
    assert len(duplicate_errors) == 2
    assert all(error.lab_id == "duplicate-lab" for error in duplicate_errors)


def test_environment_override_and_explicit_directory_precedence(tmp_path: Path) -> None:
    override = tmp_path / "override"
    explicit = tmp_path / "explicit"
    write_lab(override, "env", lab_data("environment-lab"), pod_manifest("environment-lab"))
    write_lab(explicit, "arg", lab_data("explicit-lab"), pod_manifest("explicit-lab"))

    environment_snapshot = LabRegistry(environ={"KUBELAB_LABS_DIR": str(override)}).scan()
    explicit_snapshot = LabRegistry(explicit, environ={"KUBELAB_LABS_DIR": str(override)}).scan()

    assert environment_snapshot.labs[0].definition.metadata.id == "environment-lab"
    assert explicit_snapshot.labs[0].definition.metadata.id == "explicit-lab"


@pytest.mark.parametrize("override", ["relative/labs", "missing-absolute"])
def test_invalid_registry_root_returns_structured_error(tmp_path: Path, override: str) -> None:
    value = override if override.startswith("relative") else str(tmp_path / override)

    snapshot = LabRegistry(environ={"KUBELAB_LABS_DIR": value}).scan()

    assert snapshot.labs == ()
    assert snapshot.errors[0].code is RegistryErrorCode.LABS_DIR_INVALID
    assert snapshot.errors[0].retryable is True


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.yaml",
        "manifests/../../outside.yaml",
        r"..\outside.yaml",
        r"C:\Windows\system.ini",
        "C:/Windows/system.ini",
        r"\\server\share\manifest.yaml",
        "./manifest.yaml",
        "manifests//pod.yaml",
    ],
)
def test_portable_path_policy_rejects_windows_and_linux_escapes(path: str) -> None:
    assert _portable_relative_path(path) is False


def test_portable_path_policy_accepts_posix_relative_path() -> None:
    assert _portable_relative_path("manifests/workload.yaml") is True


@pytest.mark.parametrize("reference", ["/tmp/pod.yaml", "../pod.yaml", r"..\pod.yaml"])
def test_registry_reports_path_escape(reference: str, tmp_path: Path) -> None:
    write_lab(tmp_path, "escape", lab_data("escape-lab", reference), None)

    snapshot = LabRegistry(tmp_path).scan()

    assert codes(snapshot) == {RegistryErrorCode.LAB_PATH_ESCAPE}
    assert snapshot.errors[0].lab_id == "escape-lab"


def test_missing_manifest_is_structured(tmp_path: Path) -> None:
    write_lab(tmp_path, "missing", lab_data("missing-lab"), None)

    snapshot = LabRegistry(tmp_path).scan()

    assert codes(snapshot) == {RegistryErrorCode.LAB_MANIFEST_MISSING}


def test_internal_symlink_is_allowed_and_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(yaml.safe_dump(pod_manifest("outside")), encoding="utf-8")
    internal = write_lab(tmp_path, "internal", lab_data("internal-lab"), None)
    target = internal / "real.yaml"
    target.write_text(yaml.safe_dump(pod_manifest("internal-lab")), encoding="utf-8")
    escaped = write_lab(tmp_path, "escaped", lab_data("escaped-lab"), None)
    try:
        os.symlink(target, internal / "manifest.yaml")
        os.symlink(outside, escaped / "manifest.yaml")
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this Windows host")

    snapshot = LabRegistry(tmp_path).scan()

    assert [lab.definition.metadata.id for lab in snapshot.labs] == ["internal-lab"]
    assert RegistryErrorCode.LAB_PATH_ESCAPE in codes(snapshot)


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: duplicate\n  name: secret\n",
            RegistryErrorCode.MANIFEST_YAML_INVALID,
        ),
        (
            [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "one"}}, None],
            RegistryErrorCode.MANIFEST_YAML_INVALID,
        ),
        (
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "bad"}},
            RegistryErrorCode.MANIFEST_CLUSTER_SCOPED,
        ),
        (
            {"apiVersion": "example.io/v1", "kind": "Widget", "metadata": {"name": "bad"}},
            RegistryErrorCode.MANIFEST_KIND_UNSUPPORTED,
        ),
        (
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "bad", "namespace": "production"},
            },
            RegistryErrorCode.MANIFEST_NAMESPACE_FORBIDDEN,
        ),
    ],
)
def test_manifest_document_and_resource_policy(
    tmp_path: Path, manifest: Any, expected: RegistryErrorCode
) -> None:
    write_lab(tmp_path, "case", lab_data("policy-lab"), manifest)

    snapshot = LabRegistry(tmp_path).scan()

    assert expected in codes(snapshot)


def unsafe_pod_spec(case: str) -> dict[str, Any]:
    container: dict[str, Any] = {"name": "web", "image": "nginx:1.27"}
    spec: dict[str, Any] = {"containers": [container]}
    if case in {"hostNetwork", "hostPID", "hostIPC"}:
        spec[case] = True
    elif case == "hostPath":
        spec["volumes"] = [{"name": "host", "hostPath": {"path": "/etc"}}]
    elif case == "hostPort":
        container["ports"] = [{"containerPort": 80, "hostPort": 8080}]
    else:
        security: dict[str, Any] = {}
        container["securityContext"] = security
        if case in {"privileged", "allowPrivilegeEscalation"}:
            security[case] = True
        elif case == "procMount":
            security[case] = "Unmasked"
        elif case == "seccompProfile":
            security[case] = {"type": "Unconfined"}
        elif case == "hostProcess":
            security["windowsOptions"] = {"hostProcess": True}
        elif case == "capabilities":
            security[case] = {"add": ["NET_BIND_SERVICE"]}
    return spec


@pytest.mark.parametrize(
    "case",
    [
        "privileged",
        "allowPrivilegeEscalation",
        "hostNetwork",
        "hostPID",
        "hostIPC",
        "hostPath",
        "hostPort",
        "procMount",
        "seccompProfile",
        "hostProcess",
        "capabilities",
    ],
)
def test_unsafe_pod_fields_are_rejected(case: str, tmp_path: Path) -> None:
    write_lab(
        tmp_path,
        "unsafe",
        lab_data("unsafe-case"),
        pod_manifest("unsafe-case", unsafe_pod_spec(case)),
    )

    snapshot = LabRegistry(tmp_path).scan()

    assert codes(snapshot) == {RegistryErrorCode.MANIFEST_UNSAFE}
    assert snapshot.errors[0].field_path is not None


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet", "Job"])
def test_workload_pod_templates_are_scanned(kind: str, tmp_path: Path) -> None:
    manifest = {
        "apiVersion": "batch/v1" if kind == "Job" else "apps/v1",
        "kind": kind,
        "metadata": {"name": "unsafe"},
        "spec": {"template": {"spec": unsafe_pod_spec("hostNetwork")}},
    }
    write_lab(tmp_path, "unsafe", lab_data("template-lab"), manifest)

    assert codes(LabRegistry(tmp_path).scan()) == {RegistryErrorCode.MANIFEST_UNSAFE}


def test_cronjob_init_and_ephemeral_containers_are_scanned(tmp_path: Path) -> None:
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "unsafe"},
        "spec": {
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{"name": "main", "image": "busybox:1.36"}],
                            "initContainers": [
                                {
                                    "name": "init",
                                    "image": "busybox:1.36",
                                    "securityContext": {"privileged": True},
                                }
                            ],
                            "ephemeralContainers": [
                                {
                                    "name": "debug",
                                    "image": "busybox:1.36",
                                    "securityContext": {"allowPrivilegeEscalation": True},
                                }
                            ],
                        }
                    }
                }
            }
        },
    }
    write_lab(tmp_path, "unsafe", lab_data("cronjob-lab"), manifest)

    snapshot = LabRegistry(tmp_path).scan()

    assert len(snapshot.errors) == 2
    assert codes(snapshot) == {RegistryErrorCode.MANIFEST_UNSAFE}


@pytest.mark.parametrize("service_type", ["NodePort", "LoadBalancer", "ExternalName"])
def test_dangerous_service_types_are_rejected(service_type: str, tmp_path: Path) -> None:
    manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
        "spec": {"type": service_type, "ports": [{"port": 80}]},
    }
    write_lab(tmp_path, "service", lab_data("service-lab"), manifest)

    assert codes(LabRegistry(tmp_path).scan()) == {RegistryErrorCode.MANIFEST_UNSAFE}


def test_external_ips_owner_references_and_resource_limits_are_rejected(
    tmp_path: Path,
) -> None:
    manifests = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web"},
            "spec": {"externalIPs": ["203.0.113.5"], "ports": [{"port": 80}]},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "web",
                "ownerReferences": [
                    {"apiVersion": "apps/v1", "kind": "Deployment", "name": "outside"}
                ],
            },
            "spec": {
                "containers": [
                    {
                        "name": "web",
                        "image": "nginx:1.27",
                        "resources": {"limits": {"cpu": "3", "memory": "3Gi"}},
                    }
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "data"},
            "spec": {"resources": {"requests": {"storage": "3Gi"}}},
        },
    ]
    write_lab(tmp_path, "limits", lab_data("limits-lab"), manifests)

    snapshot = LabRegistry(tmp_path).scan()

    assert codes(snapshot) == {RegistryErrorCode.MANIFEST_UNSAFE}
    assert len(snapshot.errors) >= 5


def test_external_urls_and_secret_values_are_rejected_without_disclosure(tmp_path: Path) -> None:
    secret_token = "TOP-SECRET-TOKEN-DO-NOT-LEAK"
    external = f"https://example.com/api?token={secret_token}"
    manifests = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "config"},
            "data": {
                "endpoint": external,
                "database": "postgresql://db.example.com/application",
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "secret"},
            "data": {"endpoint": base64.b64encode(external.encode()).decode()},
        },
    ]
    write_lab(tmp_path, "secret", lab_data("secret-lab"), manifests)

    snapshot = LabRegistry(tmp_path).scan()
    output = snapshot.model_dump_json()

    assert codes(snapshot) == {RegistryErrorCode.MANIFEST_UNSAFE}
    assert secret_token not in output
    assert external not in output
    assert "kind: Secret" not in output


def test_internal_service_url_and_matching_namespace_are_allowed(tmp_path: Path) -> None:
    manifests = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web", "namespace": "kubelab-internal-lab"},
            "spec": {"ports": [{"port": 80}]},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "config"},
            "data": {"endpoint": "http://web:80/healthz"},
        },
    ]
    write_lab(tmp_path, "internal", lab_data("internal-lab"), manifests)

    snapshot = LabRegistry(tmp_path).scan()

    assert len(snapshot.labs) == 1
    assert snapshot.errors == ()


def test_schema_errors_are_structured_and_do_not_echo_input(tmp_path: Path) -> None:
    secret = "TOP-SECRET-TOKEN"
    data = lab_data("schema-error")
    data["unknown"] = secret
    write_lab(tmp_path, "schema", data, pod_manifest("schema-error"))

    snapshot = LabRegistry(tmp_path).scan()
    output = snapshot.model_dump_json()

    assert codes(snapshot) == {RegistryErrorCode.LAB_SCHEMA_INVALID}
    assert secret not in output
    assert snapshot.errors[0].field_path == "unknown"


def test_default_project_labs_directory_is_available() -> None:
    snapshot = LabRegistry(environ={}).scan()

    assert snapshot == snapshot.model_copy()


def test_lab_file_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "lab.yaml"
    source.write_text(yaml.safe_dump(lab_data("outside-lab")), encoding="utf-8")
    directory = tmp_path / "registry" / "linked"
    directory.mkdir(parents=True)
    try:
        os.symlink(source, directory / "lab.yaml")
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this Windows host")

    snapshot = LabRegistry(tmp_path / "registry").scan()

    assert codes(snapshot) == {RegistryErrorCode.LAB_PATH_ESCAPE}


def test_non_directory_explicit_root_is_rejected(tmp_path: Path) -> None:
    file_path = tmp_path / "labs"
    file_path.write_text("not a directory", encoding="utf-8")

    snapshot = LabRegistry(file_path).scan()

    assert codes(snapshot) == {RegistryErrorCode.LABS_DIR_INVALID}


def test_copying_fixture_preserves_linux_and_windows_compatible_layout(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "labs" / "valid"
    destination = tmp_path / "labs copied with spaces"
    shutil.copytree(source, destination)

    snapshot = LabRegistry(destination).scan()

    assert [lab.definition.metadata.id for lab in snapshot.labs] == ["complete-lab"]


def test_materialize_rereads_and_rescans_valid_lab(tmp_path: Path) -> None:
    write_lab(tmp_path, "safe", lab_data("safe-lab"), pod_manifest("safe-lab"))
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]

    materialized = registry.materialize_for_gateway(loaded)

    assert len(materialized.documents) == 1
    assert materialized.documents[0].data["kind"] == "Pod"


def test_materialize_rejects_manifest_digest_change(tmp_path: Path) -> None:
    directory = write_lab(tmp_path, "changed", lab_data("changed-lab"), pod_manifest("changed-lab"))
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(pod_manifest("other-name")), encoding="utf-8"
    )

    with pytest.raises(LabMaterializationError) as error:
        registry.materialize_for_gateway(loaded)

    assert error.value.code == "LAB_SOURCE_CHANGED"
    assert error.value.errors[0].code is RegistryErrorCode.LAB_SOURCE_CHANGED


def test_materialize_rejects_definition_change(tmp_path: Path) -> None:
    directory = write_lab(tmp_path, "changed", lab_data("changed-lab"), pod_manifest("changed-lab"))
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]
    changed = lab_data("changed-lab")
    changed["task"]["description"] = "Changed after scan."
    (directory / "lab.yaml").write_text(yaml.safe_dump(changed), encoding="utf-8")

    with pytest.raises(LabMaterializationError):
        registry.materialize_for_gateway(loaded)


def test_materialize_rescans_even_when_digest_metadata_is_spoofed(tmp_path: Path) -> None:
    directory = write_lab(tmp_path, "unsafe", lab_data("unsafe-lab"), pod_manifest("unsafe-lab"))
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]
    unsafe = pod_manifest(
        "unsafe-lab",
        {"hostNetwork": True, "containers": [{"name": "web", "image": "nginx:1.27"}]},
    )
    payload = yaml.safe_dump(unsafe).encode()
    (directory / "manifest.yaml").write_bytes(payload)
    spoofed = loaded.model_copy(update={"manifest_sha256": (hashlib.sha256(payload).hexdigest(),)})

    with pytest.raises(LabMaterializationError) as error:
        registry.materialize_for_gateway(spoofed)

    assert error.value.errors[0].code is RegistryErrorCode.MANIFEST_UNSAFE


def test_materialize_rejects_forged_lab_path_escape(tmp_path: Path) -> None:
    write_lab(tmp_path, "safe", lab_data("safe-lab"), pod_manifest("safe-lab"))
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]
    forged = loaded.model_copy(update={"lab_path": "../outside/lab.yaml"})

    with pytest.raises(LabMaterializationError) as error:
        registry.materialize_for_gateway(forged)

    assert error.value.errors[0].code is RegistryErrorCode.LAB_SOURCE_CHANGED


def test_materialize_supports_lab_at_registry_root(tmp_path: Path) -> None:
    data = lab_data("root-lab")
    (tmp_path / "lab.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump(pod_manifest("root-lab")), encoding="utf-8"
    )
    registry = LabRegistry(tmp_path)
    loaded = registry.scan().labs[0]

    assert loaded.lab_path == "lab.yaml"
    assert registry.materialize_for_gateway(loaded).documents[0].data["kind"] == "Pod"
