"""M7 learning-path schema, registry, and derived-state contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kubelab.lab_registry import LabRegistry
from kubelab.learning_paths import (
    LabLearningFacts,
    LearningFacts,
    LearningNodeState,
    LearningPathCatalogDefinition,
    LearningPathErrorCode,
    LearningPathOutcome,
    LearningPathRegistry,
    RecommendationAction,
    derive_outcome,
    evaluate_path,
    recommend_next,
    render_outcome_markdown,
)
from kubelab.schema_export import render_learning_path_json_schema

PROJECT = Path(__file__).parents[1]
CATALOG_PATH = PROJECT / "src" / "kubelab" / "content" / "learning-paths.yaml"
LAB_IDS = frozenset(lab.definition.metadata.id for lab in LabRegistry(PROJECT / "labs").scan().labs)
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _catalog() -> LearningPathCatalogDefinition:
    result = LearningPathRegistry(CATALOG_PATH).scan(LAB_IDS)
    assert result.catalog is not None, result.errors
    return result.catalog


def _raw_catalog() -> dict[str, Any]:
    value = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_catalog(tmp_path: Path, value: dict[str, Any]) -> Path:
    target = tmp_path / "learning-paths.yaml"
    target.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def test_bundled_catalog_defines_four_paths_twenty_one_cards_and_nine_symptoms() -> None:
    catalog = _catalog()

    assert tuple(path.metadata.id for path in catalog.paths) == (
        "workload-lifecycle",
        "configuration-dependencies",
        "service-discovery-traffic",
        "storage-scheduling",
    )
    assert len(catalog.knowledge_cards) == 21
    assert {card.lab_id for card in catalog.knowledge_cards} == LAB_IDS
    assert len(catalog.symptoms) == 9


def test_registry_rejects_unknown_lab_reference(tmp_path: Path) -> None:
    raw = _raw_catalog()
    raw["paths"][0]["nodes"][1]["labId"] = "lab-999-unknown"

    result = LearningPathRegistry(_write_catalog(tmp_path, raw)).scan(LAB_IDS)

    assert result.catalog is None
    assert result.errors[0].code is LearningPathErrorCode.LAB_REFERENCE_INVALID
    assert result.errors[0].field_path == "paths.0.nodes.1.labId"


def test_registry_rejects_node_dependency_cycle(tmp_path: Path) -> None:
    raw = _raw_catalog()
    raw["paths"][0]["nodes"][0]["dependsOn"] = ["scaling-baseline"]

    result = LearningPathRegistry(_write_catalog(tmp_path, raw)).scan(LAB_IDS)

    assert result.catalog is None
    assert result.errors[0].code is LearningPathErrorCode.DEPENDENCY_CYCLE
    assert result.errors[0].field_path == "paths.0.nodes"


def test_registry_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    target = tmp_path / "learning-paths.yaml"
    target.write_text(
        "apiVersion: kubelab.io/v1alpha1\n"
        "kind: LearningPathCatalog\n"
        "kind: LearningPathCatalog\n"
        "paths: []\nknowledgeCards: []\nsymptoms: []\n",
        encoding="utf-8",
    )

    result = LearningPathRegistry(target).scan(LAB_IDS)

    assert result.catalog is None
    assert result.errors[0].code is LearningPathErrorCode.CATALOG_INVALID
    assert "duplicate" not in result.errors[0].message.lower()


def test_registry_rejects_secret_manifest_and_stack_content(tmp_path: Path) -> None:
    for unsafe in (
        "Bearer private-token",
        "apiVersion: v1 kind: Secret",
        "Traceback File /private/path",
    ):
        raw = _raw_catalog()
        raw["paths"][0]["metadata"]["description"] = unsafe

        result = LearningPathRegistry(_write_catalog(tmp_path, raw)).scan(LAB_IDS)

        assert result.catalog is None
        assert result.errors[0].code is LearningPathErrorCode.CATALOG_INVALID
        assert "private-token" not in result.errors[0].model_dump_json()
        assert "/private/path" not in result.errors[0].model_dump_json()


def test_path_state_and_recommendation_are_derived_from_session_facts() -> None:
    catalog = _catalog()
    cards = {card.lab_id: card for card in catalog.knowledge_cards}
    facts = LearningFacts(
        labs={
            "lab-001-deployment-scaling": LabLearningFacts(
                lab_id="lab-001-deployment-scaling",
                baseline_completed=True,
                attempt_count=1,
                completion_count=1,
                latest_attempt_passed=True,
                first_completed_at=NOW,
                last_completed_at=NOW,
                last_practiced_at=NOW,
            ),
            "lab-015-job-command-failure": LabLearningFacts(
                lab_id="lab-015-job-command-failure",
                baseline_completed=True,
                variant_total=2,
                variant_completed=1,
                attempt_count=2,
                completion_count=2,
                latest_attempt_passed=True,
            ),
        }
    )

    details = tuple(evaluate_path(path, cards, facts) for path in catalog.paths)
    workload = details[0]
    recommendation = recommend_next(catalog.paths, details, facts)

    scaling = next(node for node in workload.nodes if node.id == "scaling-baseline")
    job_variant = next(node for node in workload.nodes if node.id == "job-variant")
    assert scaling.state is LearningNodeState.COMPLETED
    assert scaling.after is not None
    assert job_variant.state is LearningNodeState.COMPLETED
    assert recommendation.action is RecommendationAction.START_BASELINE
    assert recommendation.lab_id == "lab-002-rolling-update"
    assert "下一个" in recommendation.reason


def test_baseline_without_completed_variant_gets_explainable_review_state() -> None:
    catalog = _catalog()
    cards = {card.lab_id: card for card in catalog.knowledge_cards}
    facts = LearningFacts(
        labs={
            "lab-013-service-target-port": LabLearningFacts(
                lab_id="lab-013-service-target-port",
                baseline_completed=True,
                variant_total=2,
                variant_completed=0,
                latest_attempt_passed=True,
            )
        }
    )

    detail = evaluate_path(catalog.paths[2], cards, facts)

    baseline = next(node for node in detail.nodes if node.id == "target-port-baseline")
    variant = next(node for node in detail.nodes if node.id == "target-port-variant")
    assert baseline.state is LearningNodeState.REVIEW_RECOMMENDED
    assert variant.state is LearningNodeState.AVAILABLE


def test_composite_lock_exposes_exact_cross_path_prerequisite() -> None:
    catalog = _catalog()
    cards = {card.lab_id: card for card in catalog.knowledge_cards}
    facts = LearningFacts(
        labs={
            "lab-014-configmap-key-missing": LabLearningFacts(
                lab_id="lab-014-configmap-key-missing",
                baseline_completed=True,
                variant_total=2,
                variant_completed=1,
            )
        }
    )

    detail = evaluate_path(catalog.paths[1], cards, facts)
    composite = next(node for node in detail.nodes if node.id == "configuration-composite")

    assert composite.state is LearningNodeState.LOCKED
    assert "先完成 lab-013-service-target-port 原始基线" in composite.lock_reasons
    assert composite.after is None


def test_active_session_always_wins_recommendation() -> None:
    catalog = _catalog()
    cards = {card.lab_id: card for card in catalog.knowledge_cards}
    facts = LearningFacts(
        labs={
            "lab-014-configmap-key-missing": LabLearningFacts(
                lab_id="lab-014-configmap-key-missing"
            )
        },
        active_lab_id="lab-014-configmap-key-missing",
        active_variant_id="baseline",
    )
    details = tuple(evaluate_path(path, cards, facts) for path in catalog.paths)

    recommendation = recommend_next(catalog.paths, details, facts)

    assert recommendation.action is RecommendationAction.RESUME_SESSION
    assert recommendation.lab_id == "lab-014-configmap-key-missing"
    assert "活动Session" in recommendation.reason


def test_outcome_is_derived_without_a_second_progress_state() -> None:
    catalog = _catalog()
    facts = LearningFacts(
        labs={
            "lab-013-service-target-port": LabLearningFacts(
                lab_id="lab-013-service-target-port",
                baseline_completed=True,
                variant_total=2,
                variant_completed=1,
                hint_request_count=3,
                manual_verification_count=4,
                first_completed_at=NOW,
                last_practiced_at=NOW,
                revealed_scenarios=("命名端口不匹配",),
            )
        }
    )

    outcome = derive_outcome(catalog.paths[2], facts)

    assert outcome.path_id == "service-discovery-traffic"
    assert outcome.baseline_completed_count == 1
    assert outcome.variant_completed == 1
    assert outcome.hint_request_count == 3
    assert outcome.manual_verification_count == 4
    assert outcome.revealed_scenarios == ("命名端口不匹配",)


def test_markdown_outcome_re_redacts_and_neutralizes_dynamic_text() -> None:
    outcome = LearningPathOutcome(
        path_id="service-discovery-traffic",
        path_name='<script>alert("x")</script> Bearer secret-token',
        lab_family_count=1,
        baseline_completed_count=1,
        variant_total=1,
        variant_completed=1,
        composite_total=0,
        composite_completed=0,
        hint_request_count=0,
        manual_verification_count=1,
        first_completed_at=NOW,
        last_practiced_at=NOW,
        revealed_scenarios=("token=should-not-leak",),
        review_lab_ids=(),
    )

    markdown = render_outcome_markdown(outcome)

    assert "<script>" not in markdown
    assert "secret-token" not in markdown
    assert "should-not-leak" not in markdown
    assert "&lt;script&gt;" in markdown
    assert len(markdown) <= 32_000


def test_checked_in_learning_path_schema_matches_pydantic_model() -> None:
    schema = PROJECT / "schemas" / "learning-path-v1alpha1.schema.json"

    assert schema.read_text(encoding="utf-8") == render_learning_path_json_schema()


def test_catalog_model_rejects_duplicate_path_ids() -> None:
    raw = _raw_catalog()
    duplicate = deepcopy(raw["paths"][0])
    raw["paths"].append(duplicate)

    try:
        LearningPathCatalogDefinition.model_validate(raw)
    except ValueError as error:
        assert "path IDs must be unique" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("duplicate path ID was accepted")
