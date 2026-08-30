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
from kubelab.session_state import NewLabSession, SessionStatus, ValidationStatus
from kubelab.validation_engine import ValidationEngine
from kubelab.workspace import workspace_environment

pytestmark = pytest.mark.integration

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"
LAB_DIRECTORIES = tuple(path.name for path in sorted(LABS_ROOT.iterdir()) if path.is_dir())
VARIANT_SCENARIOS = tuple(
    (lab_dir.name, variant_dir.name)
    for lab_dir in sorted(LABS_ROOT.iterdir())
    for variant_dir in sorted((lab_dir / "variants").glob("variant-*"))
    if variant_dir.is_dir()
)
SCENARIOS = tuple((directory, "baseline") for directory in LAB_DIRECTORIES) + VARIANT_SCENARIOS
SCENARIO_BATCHES = {
    "baseline-001-012": tuple(
        scenario
        for scenario in SCENARIOS
        if scenario[1] == "baseline" and 1 <= int(scenario[0][4:7]) <= 12
    ),
    "baseline-013-021": tuple(
        scenario
        for scenario in SCENARIOS
        if scenario[1] == "baseline" and 13 <= int(scenario[0][4:7]) <= 21
    ),
    "variants-013-015": tuple(
        scenario
        for scenario in SCENARIOS
        if scenario[1] != "baseline" and 13 <= int(scenario[0][4:7]) <= 15
    ),
    "variants-016-018": tuple(
        scenario
        for scenario in SCENARIOS
        if scenario[1] != "baseline" and 16 <= int(scenario[0][4:7]) <= 18
    ),
}
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
    "lab-013-service-target-port": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-014-configmap-key-missing": ("busybox:1.36.1",),
    "lab-015-job-command-failure": ("busybox:1.36.1",),
    "lab-016-statefulset-headless": (
        "nginx:1.27-alpine",
        "curlimages/curl:8.12.1",
        "busybox:1.36.1",
    ),
    "lab-017-daemonset-node-selector": ("busybox:1.36.1",),
    "lab-018-pvc-claim-missing": ("busybox:1.36.1",),
    "lab-019-configuration-service-chain": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-020-storage-readiness-chain": ("nginx:1.27-alpine", "curlimages/curl:8.12.1"),
    "lab-021-stateful-service-chain": (
        "nginx:1.27-alpine",
        "curlimages/curl:8.12.1",
        "busybox:1.36.1",
    ),
}


def selected_scenarios(batch: str | None = None) -> tuple[tuple[str, str], ...]:
    """Resolve one fixed acceptance batch, or all scenarios for ordinary collection."""
    selected = os.environ.get("KUBELAB_LAB_INTEGRATION_BATCH") if batch is None else batch
    if selected is None or selected == "":
        return SCENARIOS
    try:
        return SCENARIO_BATCHES[selected]
    except KeyError as exc:
        choices = ", ".join(SCENARIO_BATCHES)
        raise pytest.UsageError(
            f"Unknown KUBELAB_LAB_INTEGRATION_BATCH {selected!r}; choose one of: {choices}"
        ) from exc


def test_scenario_batches_partition_all_scenarios() -> None:
    flattened = tuple(scenario for batch in SCENARIO_BATCHES.values() for scenario in batch)

    assert tuple(map(len, SCENARIO_BATCHES.values())) == (12, 9, 6, 6)
    assert len(flattened) == len(set(flattened)) == 33
    assert set(flattened) == set(SCENARIOS)


def test_scenario_batch_selection_rejects_unknown_name() -> None:
    assert selected_scenarios("variants-013-015") == SCENARIO_BATCHES["variants-013-015"]
    with pytest.raises(pytest.UsageError, match="Unknown KUBELAB_LAB_INTEGRATION_BATCH"):
        selected_scenarios("arbitrary-selection")


@pytest.mark.parametrize(
    ("directory", "variant_id"),
    selected_scenarios(),
    ids=lambda value: value,
)
def test_real_fault_repair_reset_cleanup_contract(
    tmp_path: Path, directory: str, variant_id: str
) -> None:
    _require_integration_environment()
    _require_cached_images(directory)
    _require_lab_prerequisites(directory)
    identifier = uuid4()
    namespace = f"kubelab-test-{identifier.hex[:12]}"
    fault_root = tmp_path / "fault-labs"
    fault_registry = _copy_lab(
        directory, fault_root, namespace, solution=False, variant_id=variant_id
    )
    solution_root = tmp_path / "solution-labs"
    solution_registry = _copy_lab(
        directory, solution_root, namespace, solution=True, variant_id=variant_id
    )
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
    _seed_completed_prerequisites(
        database,
        lab_id=lab.definition.metadata.id,
        variant_id=variant_id,
        namespace=namespace,
        context_name=record.name,
        context_fingerprint=fingerprint,
    )
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
        assert session.variant_id == variant_id
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
            if directory == "lab-015-job-command-failure":
                _run_workspace_kubectl(
                    workspace.kubeconfig_path,
                    "delete",
                    "job",
                    "data-check",
                    "--wait=true",
                    "--timeout=60s",
                )
            if directory == "lab-016-statefulset-headless" and variant_id == "variant-b":
                _run_workspace_kubectl(
                    workspace.kubeconfig_path,
                    "delete",
                    "statefulset",
                    "web",
                    "--wait=true",
                    "--timeout=60s",
                )
            if directory == "lab-016-statefulset-headless" and variant_id == "variant-c":
                _run_workspace_kubectl(
                    workspace.kubeconfig_path,
                    "delete",
                    "service",
                    "web-headless",
                    "--wait=true",
                    "--timeout=60s",
                )
            if directory == "lab-018-pvc-claim-missing" and variant_id == "variant-b":
                _run_workspace_kubectl(
                    workspace.kubeconfig_path,
                    "delete",
                    "persistentvolumeclaim",
                    "app-data",
                    "--wait=true",
                    "--timeout=60s",
                )
            solution_path = (
                solution_root / directory / "solutions" / "fix.yaml"
                if variant_id == "baseline"
                else solution_root / directory / "variants" / variant_id / "solutions" / "fix.yaml"
            )
            _run_workspace_kubectl(
                workspace.kubeconfig_path,
                "apply",
                "--server-side",
                "--force-conflicts",
                "--field-manager=kubelab-learner",
                "-f",
                str(solution_path),
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
    if directory in {
        "lab-012-pvc-pending",
        "lab-018-pvc-claim-missing",
        "lab-020-storage-readiness-chain",
    }:
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


def _copy_lab(
    directory: str,
    root: Path,
    namespace: str,
    *,
    solution: bool,
    variant_id: str,
) -> LabRegistry:
    destination = root / directory
    shutil.copytree(LABS_ROOT / directory, destination)
    lab_file = destination / "lab.yaml"
    lab = yaml.safe_load(lab_file.read_text(encoding="utf-8"))
    lab["environment"]["namespace"] = namespace
    if solution and variant_id == "baseline":
        lab["environment"]["manifests"] = ["solutions/fix.yaml"]
    lab_file.write_text(yaml.safe_dump(lab, sort_keys=False), encoding="utf-8")
    if solution and variant_id != "baseline":
        variant_file = destination / "variants" / variant_id / "variant.yaml"
        variant = yaml.safe_load(variant_file.read_text(encoding="utf-8"))
        variant["environment"]["manifests"] = ["solutions/fix.yaml"]
        variant_file.write_text(yaml.safe_dump(variant, sort_keys=False), encoding="utf-8")
    resource_files = tuple(
        path
        for path in destination.rglob("*.yaml")
        if "manifests" in path.parts or "solutions" in path.parts
    )
    for path in resource_files:
        documents = tuple(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        for document in documents:
            document.setdefault("metadata", {})["namespace"] = namespace
        path.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
    return LabRegistry(root)


def _seed_completed_prerequisites(
    database: Database,
    *,
    lab_id: str,
    variant_id: str,
    namespace: str,
    context_name: str,
    context_fingerprint: str,
) -> None:
    prerequisite_variants = {
        "baseline": (),
        "variant-b": ("baseline",),
        "variant-c": ("baseline", "variant-b"),
    }[variant_id]
    with database.unit_of_work() as uow:
        for completed_variant in prerequisite_variants:
            session = uow.sessions.create(
                NewLabSession(
                    id=str(uuid4()),
                    lab_id=lab_id,
                    variant_id=completed_variant,
                    namespace=namespace,
                    context_name=context_name,
                    context_fingerprint=context_fingerprint,
                )
            )
            uow.sessions.transition(session.id, SessionStatus.READY, event_type="environment_ready")
            uow.sessions.transition(
                session.id, SessionStatus.IN_PROGRESS, event_type="workspace_entered"
            )
            uow.sessions.transition(
                session.id, SessionStatus.PASSED, event_type="success_contract_passed"
            )
            uow.sessions.transition(
                session.id, SessionStatus.CLEANING, event_type="cleanup_started"
            )
            uow.sessions.transition(
                session.id, SessionStatus.COMPLETED, event_type="cleanup_completed"
            )
        uow.commit()
