"""Static and declarative author contracts for every bundled troubleshooting scenario."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from kubelab.authoring import AuthoringService
from kubelab.lab_registry import EffectiveLab, LabRegistry, LoadedLab
from kubelab.manifest_security import ManifestDocument, ManifestSecurityScanner

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
    "lab-019-configuration-service-chain",
    "lab-020-storage-readiness-chain",
    "lab-021-stateful-service-chain",
)


def _snapshot() -> tuple[LoadedLab, ...]:
    snapshot = LabRegistry(LABS_ROOT).scan()
    assert snapshot.errors == ()
    return snapshot.labs


def _variant_scenarios() -> tuple[tuple[LoadedLab, EffectiveLab], ...]:
    registry = LabRegistry(LABS_ROOT)
    return tuple(
        (parent, effective)
        for parent in registry.scan().labs
        for variant in parent.variants
        if isinstance(
            effective := registry.resolve_variant(parent, variant.definition.metadata.id),
            EffectiveLab,
        )
    )


def _yaml_documents(path: Path) -> tuple[dict[str, Any], ...]:
    values = tuple(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    assert all(isinstance(value, dict) for value in values)
    return values


def _manifest_documents(path: Path) -> tuple[ManifestDocument, ...]:
    return tuple(
        ManifestDocument(
            manifest_path=path.relative_to(LABS_ROOT).as_posix(),
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
        if "lab-013" <= lab.definition.metadata.id <= "lab-018-pvc-claim-missing"
    )
    assert all(
        lab.definition.metadata.difficulty == "advanced"
        for lab in labs
        if lab.definition.metadata.id >= "lab-019"
    )


def test_bundled_variant_catalog_has_twelve_safe_fixed_scenarios() -> None:
    scenarios = _variant_scenarios()

    assert len(scenarios) == 12
    assert all(len(effective.variant.definition.hints) == 3 for _, effective in scenarios)
    assert all(
        effective.variant.definition.hints[1].content.startswith("kubectl ")
        for _, effective in scenarios
    )
    assert all(
        effective.variant.definition.metadata.id in {"variant-b", "variant-c"}
        for _, effective in scenarios
    )


def test_all_thirty_three_scenarios_use_unified_author_contracts() -> None:
    service = AuthoringService(LABS_ROOT.parent)

    lint = service.lint(LABS_ROOT)
    lifecycle = service.test(LABS_ROOT)

    assert lint.passed, lint.issues
    assert lifecycle.passed, lifecycle.issues
    assert len(lint.scenarios) == len(lifecycle.results) == 33
    assert sum(item.scenario_type == "baseline" for item in lifecycle.results) == 18
    assert sum(item.scenario_type == "variant" for item in lifecycle.results) == 12
    assert sum(item.scenario_type == "composite" for item in lifecycle.results) == 3


@pytest.mark.parametrize("lab", _snapshot(), ids=lambda lab: lab.definition.metadata.id)
def test_standard_solutions_are_declarative_and_manifest_safe(lab: LoadedLab) -> None:
    lab_dir = LABS_ROOT / Path(lab.lab_path).parent
    paths = [lab_dir / "solutions" / "fix.yaml"]
    stage_one = lab_dir / "solutions" / "fix-stage-1.yaml"
    if stage_one.exists():
        paths.append(stage_one)

    for path in paths:
        documents = _manifest_documents(path)
        issues = ManifestSecurityScanner().scan(
            documents,
            namespace=lab.definition.environment.namespace,
        )
        assert documents
        assert issues == ()


def test_composite_labs_have_first_and_full_declarative_repairs() -> None:
    composite = tuple(
        path
        for path in LABS_ROOT.glob("lab-*/authoring.yaml")
        if path.parent.name.startswith(("lab-019-", "lab-020-", "lab-021-"))
    )
    assert len(composite) == 3
    for contract_path in composite:
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        assert contract["scenarioType"] == "composite"
        assert contract["states"]["firstRepair"]
        assert contract["repairs"]["first"]["manifest"] == "solutions/fix-stage-1.yaml"
        assert (contract_path.parent / "solutions" / "fix-stage-1.yaml").is_file()


def test_all_runtime_images_use_explicit_non_latest_tags() -> None:
    images: list[str] = []
    workload_kinds = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
    for path in sorted(LABS_ROOT.glob("lab-*/**/*.yaml")):
        if path.name in {"lab.yaml", "variant.yaml", "authoring.yaml"}:
            continue
        for document in _yaml_documents(path):
            if document.get("kind") in workload_kinds:
                images.extend(_manifest_images(document))

    assert images
    assert all(":" in image and not image.endswith(":latest") for image in images)


def _manifest_images(value: Any) -> list[str]:
    images: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                images.append(child)
            else:
                images.extend(_manifest_images(child))
    elif isinstance(value, list):
        for child in value:
            images.extend(_manifest_images(child))
    return images
