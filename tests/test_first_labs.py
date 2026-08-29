"""Static and fake-cluster contracts for the bundled troubleshooting labs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select

from kubelab.database import Database
from kubelab.db_models import CheckResultRecord, VerificationRunRecord
from kubelab.kubernetes_gateway import (
    ConfigMatchResult,
    ContainerSummary,
    HttpProbeResult,
    PodSummary,
    SessionScope,
)
from kubelab.lab_registry import LabRegistry, LoadedLab
from kubelab.lab_schema import HttpTarget
from kubelab.manifest_security import ManifestDocument, ManifestSecurityScanner
from kubelab.session_state import NewLabSession, ValidationStatus
from kubelab.validation_engine import ValidationEngine

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"
EXPECTED_IDS = (
    "lab-001-deployment-scaling",
    "lab-002-rolling-update",
    "lab-003-configmap-injection",
    "lab-004-probes",
    "lab-005-image-pull",
    "lab-006-crash-loop",
    "lab-007-service-selector",
    "lab-008-configmap-missing",
    "lab-009-readiness-path",
    "lab-010-oom-killed",
    "lab-011-ingress-backend-port",
    "lab-012-pvc-pending",
    "lab-013-service-target-port",
    "lab-014-configmap-key-missing",
    "lab-015-job-command-failure",
    "lab-016-statefulset-headless",
    "lab-017-daemonset-node-selector",
    "lab-018-pvc-claim-missing",
)
FINGERPRINT = "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class ScenarioGateway:
    """Return only safe observations for one lab before or after its standard repair."""

    def __init__(self, lab_id: str) -> None:
        self.lab_id = lab_id
        self.fixed = False

    def resource_exists(
        self, scope: SessionScope, *, api_version: str, kind: str, name: str
    ) -> bool:
        del scope, api_version, kind
        if self.lab_id == "lab-008-configmap-missing" and name == "app-settings":
            return self.fixed
        return True

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]:
        del scope, selector
        if self.lab_id == "lab-001-deployment-scaling":
            return tuple(
                _pod(
                    name=f"web-{index}",
                    image="nginx:1.27-alpine",
                    phase="Running",
                    ready=True,
                    container="web",
                )
                for index in range(3 if self.fixed else 1)
            )
        if self.lab_id == "lab-002-rolling-update":
            return tuple(
                _pod(
                    name=f"web-{index}",
                    image="nginx:1.27-alpine" if self.fixed else "nginx:1.26-alpine",
                    phase="Running",
                    ready=True,
                    container="web",
                )
                for index in range(2)
            )
        if self.lab_id == "lab-004-probes":
            return (
                _pod(
                    name="web-abc",
                    image="nginx:1.27-alpine",
                    phase="Running",
                    ready=self.fixed,
                    restarts=0 if self.fixed else 2,
                    container="web",
                ),
            )
        if self.lab_id == "lab-005-image-pull":
            return (
                _pod(
                    name="web-abc",
                    image=(
                        "nginx:1.27-alpine"
                        if self.fixed
                        else "registry.invalid/kubelab/does-not-exist:v1"
                    ),
                    phase="Running" if self.fixed else "Pending",
                    ready=self.fixed,
                    reason=None if self.fixed else "ImagePullBackOff",
                    restarts=0,
                    container="web",
                ),
            )
        if self.lab_id == "lab-006-crash-loop":
            return (
                _pod(
                    name="worker-abc",
                    image="busybox:1.36.1",
                    phase="Running",
                    ready=self.fixed,
                    reason=None if self.fixed else "CrashLoopBackOff",
                    restarts=0 if self.fixed else 2,
                    container="worker",
                ),
            )
        if self.lab_id == "lab-008-configmap-missing":
            return (
                _pod(
                    name="worker-abc",
                    image="busybox:1.36.1",
                    phase="Running" if self.fixed else "Pending",
                    ready=self.fixed,
                    reason=None if self.fixed else "CreateContainerConfigError",
                    container="worker",
                ),
            )
        if self.lab_id == "lab-009-readiness-path":
            return (
                _pod(
                    name="web-abc",
                    image="nginx:1.27-alpine",
                    phase="Running",
                    ready=self.fixed,
                    container="web",
                ),
            )
        if self.lab_id == "lab-010-oom-killed":
            return (
                _pod(
                    name="worker-abc",
                    image="busybox:1.36.1",
                    phase="Running",
                    ready=self.fixed,
                    reason=None if self.fixed else "CrashLoopBackOff",
                    restarts=0 if self.fixed else 2,
                    container="worker",
                ),
            )
        if self.lab_id == "lab-012-pvc-pending":
            return (
                _pod(
                    name="consumer-abc",
                    image="busybox:1.36.1",
                    phase="Running" if self.fixed else "Pending",
                    ready=self.fixed,
                    container="consumer",
                ),
            )
        if self.lab_id == "lab-014-configmap-key-missing":
            return (
                _pod(
                    name="worker-abc",
                    image="busybox:1.36.1",
                    phase="Running" if self.fixed else "Pending",
                    ready=self.fixed,
                    reason=None if self.fixed else "CreateContainerConfigError",
                    container="worker",
                ),
            )
        if self.lab_id == "lab-015-job-command-failure":
            return (
                _pod(
                    name="data-check-abc",
                    image="busybox:1.36.1",
                    phase="Succeeded" if self.fixed else "Failed",
                    ready=False,
                    container="checker",
                ),
            )
        if self.lab_id == "lab-016-statefulset-headless":
            return (
                _pod(
                    name="web-0",
                    image="nginx:1.27-alpine",
                    phase="Running",
                    ready=True,
                    container="web",
                ),
            )
        if self.lab_id == "lab-017-daemonset-node-selector":
            if not self.fixed:
                return ()
            return (
                _pod(
                    name="node-agent-abc",
                    image="busybox:1.36.1",
                    phase="Running",
                    ready=True,
                    container="agent",
                ),
            )
        if self.lab_id == "lab-018-pvc-claim-missing":
            return (
                _pod(
                    name="consumer-abc",
                    image="busybox:1.36.1",
                    phase="Running" if self.fixed else "Pending",
                    ready=self.fixed,
                    container="consumer",
                ),
            )
        return (
            _pod(
                name="web-abc",
                image="nginx:1.27-alpine",
                phase="Running",
                ready=True,
                container="web",
            ),
        )

    def deployment_available_replicas(self, scope: SessionScope, name: str) -> int | None:
        del scope, name
        if self.lab_id == "lab-001-deployment-scaling":
            return 3 if self.fixed else 1
        if self.lab_id == "lab-002-rolling-update":
            return 2
        if self.lab_id in {
            "lab-004-probes",
            "lab-006-crash-loop",
            "lab-008-configmap-missing",
            "lab-010-oom-killed",
            "lab-012-pvc-pending",
        }:
            return 1 if self.fixed else 0
        return 1

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None:
        del scope, name
        if self.lab_id in {
            "lab-007-service-selector",
            "lab-009-readiness-path",
            "lab-016-statefulset-headless",
        }:
            return 1 if self.fixed else 0
        return 1

    def workload_container_image(
        self,
        scope: SessionScope,
        *,
        workload_kind: str,
        workload_name: str,
        container: str,
    ) -> str | None:
        del scope, workload_kind, workload_name, container
        if self.lab_id == "lab-002-rolling-update" and not self.fixed:
            return "nginx:1.26-alpine"
        if self.lab_id == "lab-005-image-pull" and not self.fixed:
            return "registry.invalid/kubelab/does-not-exist:v1"
        return "nginx:1.27-alpine"

    def config_value_matches(
        self,
        scope: SessionScope,
        *,
        source_kind: str,
        source_name: str,
        key: str,
        expected_value: str,
    ) -> ConfigMatchResult:
        del scope, source_kind, source_name, key
        matched = True
        if self.lab_id == "lab-003-configmap-injection":
            matched = expected_value == ("production" if self.fixed else "development")
        if self.lab_id == "lab-014-configmap-key-missing":
            return ConfigMatchResult(
                resource_exists=True,
                key_exists=self.fixed,
                matched=self.fixed and expected_value == "production",
            )
        return ConfigMatchResult(resource_exists=True, key_exists=True, matched=matched)

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None:
        del scope, name
        if self.lab_id == "lab-012-pvc-pending":
            return "Bound" if self.fixed else "Pending"
        if self.lab_id == "lab-018-pvc-claim-missing":
            return "Bound" if self.fixed else None
        return "Bound"

    def run_http_probe(
        self, scope: SessionScope, target: HttpTarget, *, deadline: float
    ) -> HttpProbeResult:
        del scope, target, deadline
        if (
            self.lab_id
            in {
                "lab-007-service-selector",
                "lab-009-readiness-path",
                "lab-011-ingress-backend-port",
                "lab-013-service-target-port",
                "lab-016-statefulset-headless",
            }
            and not self.fixed
        ):
            return HttpProbeResult(status_code=None, exit_code=6)
        return HttpProbeResult(status_code=200, exit_code=0)


def _pod(
    *,
    name: str,
    image: str,
    phase: str,
    ready: bool,
    container: str,
    reason: str | None = None,
    restarts: int = 0,
) -> PodSummary:
    return PodSummary(
        name=name,
        labels={"app": "fixture"},
        phase=phase,
        ready=ready,
        restart_count=restarts,
        containers=(
            ContainerSummary(
                name=container,
                image=image,
                ready=ready,
                restart_count=restarts,
                state="waiting" if reason else "running",
                reason=reason,
            ),
        ),
    )


def _snapshot() -> tuple[LoadedLab, ...]:
    snapshot = LabRegistry(LABS_ROOT).scan()
    assert snapshot.errors == ()
    return snapshot.labs


def _yaml_documents(path: Path) -> tuple[dict[str, Any], ...]:
    values = tuple(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    assert all(isinstance(value, dict) for value in values)
    return values


def _solution_documents(lab: LoadedLab) -> tuple[ManifestDocument, ...]:
    lab_dir = LABS_ROOT / Path(lab.lab_path).parent
    path = lab_dir / "solutions" / "fix.yaml"
    return tuple(
        ManifestDocument(
            manifest_path=f"{Path(lab.lab_path).parent.as_posix()}/solutions/fix.yaml",
            document_index=index,
            data=value,
        )
        for index, value in enumerate(_yaml_documents(path))
    )


def test_default_registry_loads_expected_labs() -> None:
    labs = _snapshot()

    assert tuple(lab.definition.metadata.id for lab in labs) == EXPECTED_IDS
    assert all(len(lab.definition.hints) == 3 for lab in labs)
    assert all(len(lab.definition.interview.questions) == 3 for lab in labs)
    assert all(lab.definition.hints[1].content.startswith("kubectl ") for lab in labs)
    assert all("<" not in lab.definition.hints[1].content for lab in labs)
    assert all(
        lab.definition.metadata.difficulty == "intermediate"
        for lab in labs
        if lab.definition.metadata.id >= "lab-013"
    )


@pytest.mark.parametrize("lab", _snapshot(), ids=lambda lab: lab.definition.metadata.id)
def test_solution_fixtures_are_declarative_and_pass_manifest_safety(lab: LoadedLab) -> None:
    documents = _solution_documents(lab)

    issues = ManifestSecurityScanner().scan(
        documents,
        namespace=lab.definition.environment.namespace,
    )

    assert issues == ()
    assert documents


def test_image_pull_lab_uses_deterministic_bad_image_and_pinned_fix() -> None:
    initial = _yaml_documents(
        LABS_ROOT / "lab-005-image-pull-backoff" / "manifests" / "deployment.yaml"
    )[0]
    fixed = _yaml_documents(LABS_ROOT / "lab-005-image-pull-backoff" / "solutions" / "fix.yaml")[0]

    assert _container(initial)["image"] == "registry.invalid/kubelab/does-not-exist:v1"
    assert _container(fixed)["image"] == "nginx:1.27-alpine"


def test_crash_loop_lab_repairs_command_without_changing_image() -> None:
    initial = _yaml_documents(
        LABS_ROOT / "lab-006-crash-loop-backoff" / "manifests" / "deployment.yaml"
    )[0]
    fixed = _yaml_documents(LABS_ROOT / "lab-006-crash-loop-backoff" / "solutions" / "fix.yaml")[0]

    assert _container(initial)["image"] == _container(fixed)["image"] == "busybox:1.36.1"
    assert "exit 1" in _container(initial)["command"][-1]
    assert "while true" in _container(fixed)["command"][-1]


def test_service_selector_lab_only_repairs_the_selector() -> None:
    initial_documents = _yaml_documents(
        LABS_ROOT / "lab-007-service-selector" / "manifests" / "resources.yaml"
    )
    fixed = _yaml_documents(LABS_ROOT / "lab-007-service-selector" / "solutions" / "fix.yaml")[0]
    deployment = initial_documents[0]
    service = initial_documents[1]
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]

    assert service["spec"]["selector"] != pod_labels
    assert fixed["kind"] == "Service"
    assert fixed["spec"]["selector"] == pod_labels


def test_foundational_lab_repairs_change_only_the_intended_contract() -> None:
    scaling_initial = _yaml_documents(
        LABS_ROOT / "lab-001-deployment-scaling" / "manifests" / "deployment.yaml"
    )[0]
    scaling_fix = _yaml_documents(
        LABS_ROOT / "lab-001-deployment-scaling" / "solutions" / "fix.yaml"
    )[0]
    rollout_initial = _yaml_documents(
        LABS_ROOT / "lab-002-rolling-update" / "manifests" / "deployment.yaml"
    )[0]
    rollout_fix = _yaml_documents(LABS_ROOT / "lab-002-rolling-update" / "solutions" / "fix.yaml")[
        0
    ]
    config_initial = _yaml_documents(
        LABS_ROOT / "lab-003-configmap-injection" / "manifests" / "resources.yaml"
    )
    config_fix = _yaml_documents(
        LABS_ROOT / "lab-003-configmap-injection" / "solutions" / "fix.yaml"
    )
    probe_initial = _yaml_documents(LABS_ROOT / "lab-004-probes" / "manifests" / "deployment.yaml")[
        0
    ]
    probe_fix = _yaml_documents(LABS_ROOT / "lab-004-probes" / "solutions" / "fix.yaml")[0]

    assert scaling_initial["spec"]["replicas"] == 1
    assert scaling_fix["spec"]["replicas"] == 3
    assert _container(rollout_initial)["image"] == "nginx:1.26-alpine"
    assert _container(rollout_fix)["image"] == "nginx:1.27-alpine"
    assert config_initial[0]["data"]["APP_MODE"] == "development"
    assert config_fix[0]["data"]["APP_MODE"] == "production"
    assert _container(probe_initial)["livenessProbe"]["httpGet"]["path"] == "/does-not-exist"
    assert _container(probe_fix)["livenessProbe"]["httpGet"]["path"] == "/"


def test_configuration_and_readiness_repairs_restore_missing_dependencies() -> None:
    config_initial = _yaml_documents(
        LABS_ROOT / "lab-008-configmap-missing" / "manifests" / "deployment.yaml"
    )
    config_fix = _yaml_documents(LABS_ROOT / "lab-008-configmap-missing" / "solutions" / "fix.yaml")
    readiness_initial = _yaml_documents(
        LABS_ROOT / "lab-009-readiness-path" / "manifests" / "resources.yaml"
    )[0]
    readiness_fix = _yaml_documents(
        LABS_ROOT / "lab-009-readiness-path" / "solutions" / "fix.yaml"
    )[0]

    assert all(document["kind"] != "ConfigMap" for document in config_initial)
    assert config_fix[0]["kind"] == "ConfigMap"
    assert config_fix[0]["metadata"]["name"] == "app-settings"
    assert _container(readiness_initial)["readinessProbe"]["httpGet"]["path"] == "/not-ready"
    assert _container(readiness_fix)["readinessProbe"]["httpGet"]["path"] == "/"


def test_advanced_fault_repairs_and_prerequisites_are_explicit() -> None:
    snapshot = {lab.definition.metadata.id: lab for lab in _snapshot()}
    oom_initial = _yaml_documents(
        LABS_ROOT / "lab-010-oom-killed" / "manifests" / "deployment.yaml"
    )[0]
    oom_fix = _yaml_documents(LABS_ROOT / "lab-010-oom-killed" / "solutions" / "fix.yaml")[0]
    ingress_initial = _yaml_documents(
        LABS_ROOT / "lab-011-ingress-backend-port" / "manifests" / "resources.yaml"
    )[-1]
    ingress_fix = _yaml_documents(
        LABS_ROOT / "lab-011-ingress-backend-port" / "solutions" / "fix.yaml"
    )[0]
    pvc_initial = _yaml_documents(
        LABS_ROOT / "lab-012-pvc-pending" / "manifests" / "resources.yaml"
    )[0]
    pvc_fix = _yaml_documents(LABS_ROOT / "lab-012-pvc-pending" / "solutions" / "fix.yaml")[0]

    assert _container(oom_initial)["resources"]["limits"]["memory"] == "16Mi"
    assert _container(oom_fix)["resources"]["limits"]["memory"] == "128Mi"
    assert snapshot["lab-011-ingress-backend-port"].definition.requirements.addons == ("ingress",)
    assert snapshot["lab-012-pvc-pending"].definition.requirements.addons == (
        "default-storageclass",
    )
    assert _ingress_backend_port(ingress_initial) == 81
    assert _ingress_backend_port(ingress_fix) == 80
    assert pvc_initial["spec"]["storageClassName"] == "kubelab-missing-storage-class"
    assert pvc_fix["spec"]["storageClassName"] == "standard"


def test_intermediate_service_config_and_job_repairs_are_narrow() -> None:
    service_initial = _yaml_documents(
        LABS_ROOT / "lab-013-service-target-port" / "manifests" / "resources.yaml"
    )[-1]
    service_fix = _yaml_documents(
        LABS_ROOT / "lab-013-service-target-port" / "solutions" / "fix.yaml"
    )[0]
    config_initial = _yaml_documents(
        LABS_ROOT / "lab-014-configmap-key-missing" / "manifests" / "resources.yaml"
    )[0]
    config_fix = _yaml_documents(
        LABS_ROOT / "lab-014-configmap-key-missing" / "solutions" / "fix.yaml"
    )[0]
    job_initial = _yaml_documents(
        LABS_ROOT / "lab-015-job-command-failure" / "manifests" / "job.yaml"
    )[0]
    job_fix = _yaml_documents(LABS_ROOT / "lab-015-job-command-failure" / "solutions" / "fix.yaml")[
        0
    ]

    assert service_initial["spec"]["selector"] == service_fix["spec"]["selector"]
    assert service_initial["spec"]["ports"][0]["targetPort"] == 8080
    assert service_fix["spec"]["ports"][0]["targetPort"] == "http"
    assert config_initial["data"] == {"LOG_LEVEL": "info"}
    assert config_fix["data"] == {"LOG_LEVEL": "info", "APP_MODE": "production"}
    assert job_initial["spec"]["backoffLimit"] == job_fix["spec"]["backoffLimit"] == 0
    assert job_initial["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert job_fix["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert "exit 1" in _workload_container(job_initial)["command"][-1]
    assert "exit 0" in _workload_container(job_fix)["command"][-1]


def test_intermediate_controller_and_storage_repairs_are_narrow() -> None:
    stateful_initial = _yaml_documents(
        LABS_ROOT / "lab-016-statefulset-headless" / "manifests" / "resources.yaml"
    )
    stateful_fix = _yaml_documents(
        LABS_ROOT / "lab-016-statefulset-headless" / "solutions" / "fix.yaml"
    )[0]
    headless_service, statefulset = stateful_initial
    pod_labels = statefulset["spec"]["template"]["metadata"]["labels"]
    daemon_initial = _yaml_documents(
        LABS_ROOT / "lab-017-daemonset-node-selector" / "manifests" / "daemonset.yaml"
    )[0]
    daemon_fix = _yaml_documents(
        LABS_ROOT / "lab-017-daemonset-node-selector" / "solutions" / "fix.yaml"
    )[0]
    pvc_initial = _yaml_documents(
        LABS_ROOT / "lab-018-pvc-claim-missing" / "manifests" / "deployment.yaml"
    )
    pvc_fix = _yaml_documents(LABS_ROOT / "lab-018-pvc-claim-missing" / "solutions" / "fix.yaml")[0]

    assert headless_service["spec"]["clusterIP"] == stateful_fix["spec"]["clusterIP"] == "None"
    assert headless_service["spec"]["selector"] != pod_labels
    assert stateful_fix["spec"]["selector"] == pod_labels
    assert daemon_initial["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "kubelab-never-match"
    }
    assert daemon_fix["spec"]["template"]["spec"]["nodeSelector"] is None
    assert _workload_container(daemon_initial) == _workload_container(daemon_fix)
    assert all(document["kind"] != "PersistentVolumeClaim" for document in pvc_initial)
    assert pvc_fix["kind"] == "PersistentVolumeClaim"
    assert pvc_fix["metadata"]["name"] == "app-data"
    assert pvc_fix["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc_fix["spec"]["storageClassName"] == "standard"
    assert pvc_fix["spec"]["resources"]["requests"]["storage"] == "128Mi"


def test_all_runtime_images_use_explicit_non_latest_tags() -> None:
    images = []
    for path in sorted(LABS_ROOT.glob("lab-*/**/*.yaml")):
        if path.name == "lab.yaml":
            continue
        for document in _yaml_documents(path):
            if document.get("kind") in {
                "Pod",
                "Deployment",
                "StatefulSet",
                "DaemonSet",
                "Job",
                "CronJob",
            }:
                images.append(str(_workload_container(document)["image"]))

    assert images
    assert all(":" in image and not image.endswith(":latest") for image in images)


@pytest.mark.parametrize("lab", _snapshot(), ids=lambda lab: lab.definition.metadata.id)
def test_fault_repair_reset_contract_is_proven_and_persisted(
    tmp_path: Path, lab: LoadedLab
) -> None:
    session_id = str(uuid4())
    database = Database(tmp_path / lab.definition.metadata.id / "kubelab.db")
    database.initialize()
    with database.unit_of_work() as uow:
        uow.sessions.create(
            NewLabSession(
                id=session_id,
                lab_id=lab.definition.metadata.id,
                namespace=lab.definition.environment.namespace,
                context_name="minikube",
                context_fingerprint=FINGERPRINT,
            )
        )
        uow.commit()
    scope = SessionScope(
        lab_id=lab.definition.metadata.id,
        session_id=session_id,
        namespace=lab.definition.environment.namespace,
        context_fingerprint=FINGERPRINT,
    )
    gateway = ScenarioGateway(lab.definition.metadata.id)
    clock = FakeClock()
    engine = ValidationEngine(
        database.unit_of_work,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 26, tzinfo=UTC),
    )
    try:
        initial = engine.validate_initial_contract(scope, lab, gateway, reset_sequence=0)
        gateway.fixed = True
        repaired = engine.validate_success_contract(scope, lab, gateway, reset_sequence=0)
        gateway.fixed = False
        reset = engine.validate_initial_contract(scope, lab, gateway, reset_sequence=1)

        assert initial.status is ValidationStatus.PASSED
        assert repaired.status is ValidationStatus.PASSED
        assert reset.status is ValidationStatus.PASSED
        with database.session_factory() as session:
            runs = session.scalars(select(VerificationRunRecord)).all()
            results = session.scalars(select(CheckResultRecord)).all()
        assert len(runs) == 5
        assert {run.reset_sequence for run in runs} == {0, 1}
        assert [(run.purpose, run.status) for run in runs].count(("initial", "passed")) == 2
        assert [(run.purpose, run.status) for run in runs].count(
            ("success_contract", "failed")
        ) == 2
        assert [(run.purpose, run.status) for run in runs].count(("manual", "passed")) == 1
        assert all(result.status in {"passed", "failed"} for result in results)
    finally:
        database.dispose()


def _container(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _workload_container(workload: Mapping[str, Any]) -> Mapping[str, Any]:
    kind = workload["kind"]
    if kind == "Pod":
        pod_spec = workload["spec"]
    elif kind == "CronJob":
        pod_spec = workload["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    else:
        pod_spec = workload["spec"]["template"]["spec"]
    return pod_spec["containers"][0]


def _ingress_backend_port(ingress: Mapping[str, Any]) -> int:
    return ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"]
