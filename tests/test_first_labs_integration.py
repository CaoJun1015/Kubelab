"""Opt-in end-to-end contracts for all labs in trusted local minikube."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from kubelab.config import load_config, resolve_kubeconfig_path
from kubelab.context_trust import build_context_trust_service, trusted_context_fingerprint
from kubelab.database import Database
from kubelab.kubernetes_gateway import KubernetesGateway, SessionScope
from kubelab.lab_manager import LabManager
from kubelab.lab_registry import LabRegistry
from kubelab.operation_lock import OperationLock
from kubelab.session_state import SessionStatus, ValidationStatus
from kubelab.validation_engine import ValidationEngine
from kubelab.workspace import workspace_environment

pytestmark = pytest.mark.integration

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"
LAB_DIRECTORIES = tuple(path.name for path in sorted(LABS_ROOT.iterdir()) if path.is_dir())
REQUIRED_IMAGES = {
    "lab-001-deployment-scaling": ("nginx:1.27-alpine",),
    "lab-002-rolling-update": ("nginx:1.26-alpine", "nginx:1.27-alpine"),
    "lab-003-configmap-injection": ("busybox:1.36.1",),
    "lab-004-probes": ("nginx:1.27-alpine",),
    "lab-005-image-pull-backoff": ("nginx:1.27-alpine",),
    "lab-006-crash-loop-backoff": ("busybox:1.36.1",),
    "lab-007-service-selector": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-008-configmap-missing": ("busybox:1.36.1",),
    "lab-009-readiness-path": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-010-oom-killed": ("busybox:1.36.1",),
    "lab-011-ingress-backend-port": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-012-pvc-pending": ("busybox:1.36.1",),
}


@pytest.mark.parametrize("directory", LAB_DIRECTORIES)
def test_real_fault_repair_reset_cleanup_contract(tmp_path: Path, directory: str) -> None:
    _require_integration_environment()
    _require_cached_images(directory)
    _require_lab_prerequisites(directory)
    identifier = uuid4()
    namespace = f"kubelab-test-{identifier.hex[:12]}"
    fault_root = tmp_path / "fault-labs"
    fault_registry = _copy_lab(directory, fault_root, namespace, solution=False)
    solution_root = tmp_path / "solution-labs"
    solution_registry = _copy_lab(directory, solution_root, namespace, solution=True)
    fault_snapshot = fault_registry.scan()
    solution_snapshot = solution_registry.scan()
    assert fault_snapshot.errors == ()
    assert solution_snapshot.errors == ()
    lab = fault_snapshot.labs[0]

    trust = build_context_trust_service()
    record = trust.assert_trusted_context()
    fingerprint = trusted_context_fingerprint(record)
    local_config = load_config()
    kubeconfig = resolve_kubeconfig_path(local_config)

    def gateway_factory(trusted: Any, context_fingerprint: str) -> KubernetesGateway:
        return KubernetesGateway.from_kubeconfig(
            kubeconfig_path=kubeconfig,
            context_name=trusted.name,
            context_fingerprint=context_fingerprint,
        )

    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    manager = LabManager(
        registry=fault_registry,
        unit_of_work=database.unit_of_work,
        operation_lock=OperationLock(tmp_path / "manager.lock", timeout_seconds=0),
        context_trust=trust,
        gateway_factory=gateway_factory,
        validation=ValidationEngine(database.unit_of_work),
    )
    session = None
    try:
        session = manager.start(lab.definition.metadata.id)
        assert session.status is SessionStatus.READY
        with workspace_environment(
            manager,
            kubeconfig,
            temporary_root=tmp_path,
        ) as workspace:
            assert _workspace_can_i(workspace.kubeconfig_path, "patch", "deployments") is True
            assert _workspace_can_i(workspace.kubeconfig_path, "get", "secrets") is False
            assert _workspace_can_i(workspace.kubeconfig_path, "get", "namespaces") is False
            if directory == "lab-012-pvc-pending":
                _run_workspace_kubectl(
                    workspace.kubeconfig_path,
                    "delete",
                    "persistentvolumeclaim",
                    "data",
                    "--wait=true",
                    "--timeout=60s",
                )
            _run_workspace_kubectl(
                workspace.kubeconfig_path,
                "apply",
                "--server-side",
                "--force-conflicts",
                "--field-manager=kubelab-learner",
                "-f",
                str(solution_root / directory / "solutions" / "fix.yaml"),
            )

        verified = manager.verify(session.id)
        assert verified.status is ValidationStatus.PASSED
        reset = manager.reset(session.id)
        assert reset.status is SessionStatus.READY
        assert reset.reset_count == 1
        completed = manager.cleanup(session.id)
        assert completed.status is SessionStatus.COMPLETED
    finally:
        if session is not None:
            try:
                manager.cleanup(session.id)
            except Exception:
                gateway = gateway_factory(record, fingerprint)
                try:
                    scope = SessionScope(
                        lab_id=session.lab_id,
                        session_id=session.id,
                        namespace=session.namespace,
                        context_fingerprint=fingerprint,
                    )
                    if gateway.namespace_exists(scope):
                        gateway.delete_environment(scope)
                finally:
                    gateway.close()
        database.dispose()

    verification_gateway = gateway_factory(record, fingerprint)
    try:
        scope = SessionScope(
            lab_id=lab.definition.metadata.id,
            session_id=str(identifier),
            namespace=namespace,
            context_fingerprint=fingerprint,
        )
        assert verification_gateway.namespace_exists(scope) is False
    finally:
        verification_gateway.close()


def _require_integration_environment() -> None:
    if os.environ.get("KUBELAB_RUN_LAB_INTEGRATION") != "1":
        pytest.skip("Set KUBELAB_RUN_LAB_INTEGRATION=1 to run real troubleshooting contracts")
    if not os.environ.get("WSL_DISTRO_NAME"):
        pytest.skip("Real lab contracts are supported only inside WSL2 Ubuntu")


def _require_cached_images(directory: str) -> None:
    try:
        completed = subprocess.run(
            ["minikube", "image", "ls", "--profile", "minikube"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"Could not inspect minikube image cache: {type(exc).__name__}")
    missing = [
        image
        for image in REQUIRED_IMAGES[directory]
        if not any(line.endswith(image) for line in completed.stdout.splitlines())
    ]
    if missing:
        pytest.skip(f"Required fixed-version images are not cached in minikube: {missing}")


def _require_lab_prerequisites(directory: str) -> None:
    if directory == "lab-011-ingress-backend-port":
        _require_kubectl_value(
            ("-n", "ingress-nginx", "get", "deployment", "ingress-nginx-controller"),
            "Ingress controller is unavailable",
        )
    if directory == "lab-012-pvc-pending":
        _require_kubectl_value(
            ("get", "storageclass", "standard"),
            "The standard StorageClass is unavailable",
        )


def _require_kubectl_value(arguments: tuple[str, ...], reason: str) -> None:
    try:
        subprocess.run(
            ["kubectl", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"{reason}: {type(exc).__name__}")


def _run_workspace_kubectl(kubeconfig: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
    )
    if result.returncode != 0:
        pytest.fail(f"restricted workspace kubectl failed: {result.stderr.strip()}")


def _workspace_can_i(kubeconfig: Path, verb: str, resource: str) -> bool:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "auth", "can-i", verb, resource],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    return result.stdout.strip() == "yes"


def _copy_lab(directory: str, root: Path, namespace: str, *, solution: bool) -> LabRegistry:
    destination = root / directory
    shutil.copytree(LABS_ROOT / directory, destination)
    lab_file = destination / "lab.yaml"
    lab = yaml.safe_load(lab_file.read_text(encoding="utf-8"))
    lab["environment"]["namespace"] = namespace
    if solution:
        lab["environment"]["manifests"] = ["solutions/fix.yaml"]
    lab_file.write_text(yaml.safe_dump(lab, sort_keys=False), encoding="utf-8")
    for path in (*destination.glob("manifests/*.yaml"), *destination.glob("solutions/*.yaml")):
        documents = tuple(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        for document in documents:
            document.setdefault("metadata", {})["namespace"] = namespace
        path.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    return LabRegistry(root)
