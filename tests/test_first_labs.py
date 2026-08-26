"""Static and fake-cluster contracts for the first three troubleshooting labs."""

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
    "lab-005-image-pull",
    "lab-006-crash-loop",
    "lab-007-service-selector",
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
        del scope, api_version, kind, name
        return True

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]:
        del scope, selector
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
        if self.lab_id == "lab-006-crash-loop":
            return 1 if self.fixed else 0
        return 1

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None:
        del scope, name
        if self.lab_id == "lab-007-service-selector":
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
        del scope, source_kind, source_name, key, expected_value
        return ConfigMatchResult(resource_exists=True, key_exists=True, matched=True)

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None:
        del scope, name
        return "Bound"

    def run_http_probe(
        self, scope: SessionScope, target: HttpTarget, *, deadline: float
    ) -> HttpProbeResult:
        del scope, target, deadline
        if self.lab_id == "lab-007-service-selector" and not self.fixed:
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


def test_default_registry_loads_exactly_the_first_three_labs() -> None:
    labs = _snapshot()

    assert tuple(lab.definition.metadata.id for lab in labs) == EXPECTED_IDS
    assert all(len(lab.definition.hints) == 3 for lab in labs)
    assert all(len(lab.definition.interview.questions) == 3 for lab in labs)


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


def test_all_runtime_images_use_explicit_non_latest_tags() -> None:
    images = []
    for path in sorted(LABS_ROOT.glob("lab-*/**/*.yaml")):
        if path.name == "lab.yaml":
            continue
        for document in _yaml_documents(path):
            if document.get("kind") == "Deployment":
                images.append(str(_container(document)["image"]))

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
