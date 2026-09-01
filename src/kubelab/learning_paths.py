"""Declarative M7 learning paths and pure derived learning-state evaluation."""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from kubelab.redaction import redact_json

LEARNING_PATH_API_VERSION = "kubelab.io/v1alpha1"
LEARNING_PATH_KIND = "LearningPathCatalog"

Slug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", min_length=1, max_length=63),
]
LabId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,39}$", min_length=3, max_length=40),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class LearningPathModel(BaseModel):
    """Strict immutable base shared by definitions and public DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LearningRequirement(LearningPathModel):
    """A prerequisite derived from existing Session success events."""

    kind: Literal["baseline_completed", "variant_completed"]
    lab_id: LabId = Field(alias="labId")
    minimum_variant_count: int | None = Field(
        alias="minimumVariantCount", default=None, ge=1, le=20
    )

    @model_validator(mode="after")
    def variant_count_matches_kind(self) -> LearningRequirement:
        if self.kind == "variant_completed" and self.minimum_variant_count is None:
            raise ValueError("variant_completed requires minimumVariantCount")
        if self.kind == "baseline_completed" and self.minimum_variant_count is not None:
            raise ValueError("baseline_completed forbids minimumVariantCount")
        return self


class LearningPathNodeDefinition(LearningPathModel):
    id: Slug
    type: Literal["concept", "baseline", "variant", "composite", "retrospective"]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    description: NonEmptyText
    lab_id: LabId | None = Field(alias="labId", default=None)
    depends_on: tuple[Slug, ...] = Field(alias="dependsOn", default=())
    requirements: tuple[LearningRequirement, ...] = ()

    @model_validator(mode="after")
    def lab_binding_matches_type(self) -> LearningPathNodeDefinition:
        lab_node = self.type in {"baseline", "variant", "composite"}
        if lab_node != (self.lab_id is not None):
            raise ValueError("labId is required only for baseline, variant, and composite nodes")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("dependsOn values must be unique")
        return self


class LearningPathMetadata(LearningPathModel):
    id: Slug
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: NonEmptyText
    objective: NonEmptyText


class LearningPathDefinition(LearningPathModel):
    metadata: LearningPathMetadata
    nodes: tuple[LearningPathNodeDefinition, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def node_contract_is_consistent(self) -> LearningPathDefinition:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node IDs must be unique within one path")
        if sum(node.type == "concept" for node in self.nodes) != 1:
            raise ValueError("each path requires exactly one concept node")
        if sum(node.type == "retrospective" for node in self.nodes) != 1:
            raise ValueError("each path requires exactly one retrospective node")
        return self


class BeforeKnowledgeCard(LearningPathModel):
    what: NonEmptyText
    why: NonEmptyText
    success_goal: NonEmptyText = Field(alias="successGoal")
    objects: tuple[NonEmptyText, ...] = Field(min_length=1)
    evidence_checklist: tuple[NonEmptyText, ...] = Field(alias="evidenceChecklist", min_length=1)


class AfterKnowledgeCard(LearningPathModel):
    key_evidence: NonEmptyText = Field(alias="keyEvidence")
    root_cause: NonEmptyText = Field(alias="rootCause")
    minimal_fix: NonEmptyText = Field(alias="minimalFix")
    anti_patterns: tuple[NonEmptyText, ...] = Field(alias="antiPatterns", min_length=1)
    prevention: NonEmptyText


class LabKnowledgeCard(LearningPathModel):
    lab_id: LabId = Field(alias="labId")
    before: BeforeKnowledgeCard
    after: AfterKnowledgeCard


class SymptomDefinition(LearningPathModel):
    id: Slug
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: NonEmptyText
    evidence_order: tuple[NonEmptyText, ...] = Field(alias="evidenceOrder", min_length=1)
    cause_categories: tuple[NonEmptyText, ...] = Field(alias="causeCategories", min_length=1)
    lab_ids: tuple[LabId, ...] = Field(alias="labIds", min_length=1)


class LearningPathCatalogDefinition(LearningPathModel):
    api_version: Literal["kubelab.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LearningPathCatalog"]
    paths: tuple[LearningPathDefinition, ...] = Field(min_length=1)
    knowledge_cards: tuple[LabKnowledgeCard, ...] = Field(alias="knowledgeCards", min_length=1)
    symptoms: tuple[SymptomDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def catalog_ids_are_unique(self) -> LearningPathCatalogDefinition:
        for label, values in (
            ("path", [path.metadata.id for path in self.paths]),
            ("knowledge card", [card.lab_id for card in self.knowledge_cards]),
            ("symptom", [symptom.id for symptom in self.symptoms]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique")
        return self


class LearningPathErrorCode(StrEnum):
    CATALOG_MISSING = "LEARNING_PATH_CATALOG_MISSING"
    CATALOG_INVALID = "LEARNING_PATH_CATALOG_INVALID"
    LAB_REFERENCE_INVALID = "LEARNING_PATH_LAB_REFERENCE_INVALID"
    NODE_REFERENCE_INVALID = "LEARNING_PATH_NODE_REFERENCE_INVALID"
    DEPENDENCY_CYCLE = "LEARNING_PATH_DEPENDENCY_CYCLE"


class LearningPathRegistryError(LearningPathModel):
    code: LearningPathErrorCode
    message: str
    field_path: str | None = None


class LearningPathRegistrySnapshot(LearningPathModel):
    catalog: LearningPathCatalogDefinition | None
    errors: tuple[LearningPathRegistryError, ...] = ()


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class LearningPathRegistry:
    """Load the bundled static path catalog without executing commands."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        self._catalog_path = catalog_path or (
            Path(__file__).resolve().parent / "content" / "learning-paths.yaml"
        )

    def scan(self, lab_ids: frozenset[str]) -> LearningPathRegistrySnapshot:
        path = self._catalog_path
        if not path.is_file():
            return _registry_failure(
                LearningPathErrorCode.CATALOG_MISSING,
                "The learning path catalog is unavailable.",
            )
        try:
            if path.stat().st_size > 512_000:
                raise ValueError("catalog exceeds the size limit")
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
            if not isinstance(raw, dict):
                raise ValueError("catalog root must be a mapping")
            catalog = LearningPathCatalogDefinition.model_validate(raw)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError, ValidationError) as exc:
            field_path = _validation_field_path(exc)
            return _registry_failure(
                LearningPathErrorCode.CATALOG_INVALID,
                "The learning path catalog is invalid.",
                field_path=field_path,
            )

        reference_error = _validate_catalog_references(catalog, lab_ids)
        if reference_error is not None:
            return LearningPathRegistrySnapshot(catalog=None, errors=(reference_error,))
        return LearningPathRegistrySnapshot(catalog=catalog)


class LearningNodeState(StrEnum):
    AVAILABLE = "available"
    ACTIVE = "active"
    COMPLETED = "completed"
    LOCKED = "locked"
    REVIEW_RECOMMENDED = "review_recommended"


class LabLearningFacts(LearningPathModel):
    lab_id: str
    baseline_completed: bool = False
    variant_total: int = Field(default=0, ge=0)
    variant_completed: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    completion_count: int = Field(default=0, ge=0)
    latest_attempt_passed: bool | None = None
    latest_unlocked_hint_count: int = Field(default=0, ge=0)
    latest_manual_verification_count: int = Field(default=0, ge=0)
    hint_request_count: int = Field(default=0, ge=0)
    manual_verification_count: int = Field(default=0, ge=0)
    first_completed_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_practiced_at: datetime | None = None
    revealed_scenarios: tuple[str, ...] = ()


class LearningFacts(LearningPathModel):
    labs: dict[str, LabLearningFacts]
    active_lab_id: str | None = None
    active_variant_id: str | None = None


class LearningPathNode(LearningPathModel):
    id: str
    type: str
    title: str
    description: str
    lab_id: str | None
    state: LearningNodeState
    lock_reasons: tuple[str, ...] = ()
    before: BeforeKnowledgeCard | None = None
    after: AfterKnowledgeCard | None = None


class LearningPathSummary(LearningPathModel):
    id: str
    name: str
    description: str
    objective: str
    lab_node_count: int
    completed_node_count: int
    progress_percent: int
    current_node_id: str | None = None
    review_recommended_count: int = 0


class LearningPathDetail(LearningPathModel):
    summary: LearningPathSummary
    nodes: tuple[LearningPathNode, ...]


class LearningPathCatalogReport(LearningPathModel):
    paths: tuple[LearningPathSummary, ...]
    invalid_path_count: int = 0


class RecommendationAction(StrEnum):
    RESUME_SESSION = "resume_session"
    START_BASELINE = "start_baseline"
    PRACTICE_VARIANT = "practice_variant"
    START_COMPOSITE = "start_composite"
    REVIEW = "review"
    EXPLORE_PATH = "explore_path"
    COMPLETE = "complete"


class LearningRecommendation(LearningPathModel):
    action: RecommendationAction
    title: str
    reason: str
    path_id: str | None = None
    node_id: str | None = None
    lab_id: str | None = None


class SymptomCatalog(LearningPathModel):
    symptoms: tuple[SymptomDefinition, ...]


class LearningPathOutcome(LearningPathModel):
    path_id: str
    path_name: str
    lab_family_count: int
    baseline_completed_count: int
    variant_total: int
    variant_completed: int
    composite_total: int
    composite_completed: int
    hint_request_count: int
    manual_verification_count: int
    first_completed_at: datetime | None
    last_practiced_at: datetime | None
    revealed_scenarios: tuple[str, ...]
    review_lab_ids: tuple[str, ...]


def evaluate_path(
    definition: LearningPathDefinition,
    cards: Mapping[str, LabKnowledgeCard],
    facts: LearningFacts,
) -> LearningPathDetail:
    """Derive one path solely from existing learning facts."""
    completed_by_node = {
        node.id: _node_completed(node, facts.labs.get(node.lab_id or ""))
        for node in definition.nodes
    }
    nodes: list[LearningPathNode] = []
    for node in definition.nodes:
        fact = facts.labs.get(node.lab_id or "")
        lock_reasons = _lock_reasons(node, definition, completed_by_node, facts)
        completed = completed_by_node[node.id]
        if _node_active(node, facts):
            state = LearningNodeState.ACTIVE
        elif completed:
            state = (
                LearningNodeState.REVIEW_RECOMMENDED
                if fact is not None and _review_recommended(fact)
                else LearningNodeState.COMPLETED
            )
        elif lock_reasons:
            state = LearningNodeState.LOCKED
        else:
            state = LearningNodeState.AVAILABLE
        card = cards.get(node.lab_id or "")
        nodes.append(
            LearningPathNode(
                id=node.id,
                type=node.type,
                title=node.title,
                description=node.description,
                lab_id=node.lab_id,
                state=state,
                lock_reasons=lock_reasons,
                before=card.before if card is not None else None,
                after=card.after if card is not None and completed else None,
            )
        )

    lab_nodes = [node for node in nodes if node.lab_id is not None]
    completed_count = sum(
        node.state in {LearningNodeState.COMPLETED, LearningNodeState.REVIEW_RECOMMENDED}
        for node in lab_nodes
    )
    current = next((node.id for node in nodes if node.state is LearningNodeState.ACTIVE), None)
    if current is None:
        current = next(
            (
                node.id
                for node in nodes
                if node.lab_id is not None and node.state is LearningNodeState.AVAILABLE
            ),
            None,
        )
    total = len(lab_nodes)
    metadata = definition.metadata
    return LearningPathDetail(
        summary=LearningPathSummary(
            id=metadata.id,
            name=metadata.name,
            description=metadata.description,
            objective=metadata.objective,
            lab_node_count=total,
            completed_node_count=completed_count,
            progress_percent=round(completed_count * 100 / total) if total else 0,
            current_node_id=current,
            review_recommended_count=sum(
                node.state is LearningNodeState.REVIEW_RECOMMENDED for node in lab_nodes
            ),
        ),
        nodes=tuple(nodes),
    )


def recommend_next(
    definitions: tuple[LearningPathDefinition, ...],
    details: tuple[LearningPathDetail, ...],
    facts: LearningFacts,
) -> LearningRecommendation:
    """Return one deterministic, explainable next action."""
    if facts.active_lab_id is not None:
        for detail in details:
            match = next(
                (node for node in detail.nodes if node.state is LearningNodeState.ACTIVE), None
            )
            if match is not None:
                return LearningRecommendation(
                    action=RecommendationAction.RESUME_SESSION,
                    title=f"继续：{match.title}",
                    reason="存在尚未结束的活动Session，应先恢复调查或安全清理。",
                    path_id=detail.summary.id,
                    node_id=match.id,
                    lab_id=match.lab_id,
                )
        return LearningRecommendation(
            action=RecommendationAction.RESUME_SESSION,
            title="继续当前实验",
            reason="存在尚未结束的活动Session，应先恢复调查或安全清理。",
            lab_id=facts.active_lab_id,
        )

    ranked = sorted(
        zip(definitions, details, strict=True),
        key=lambda item: (-item[1].summary.completed_node_count, list(definitions).index(item[0])),
    )
    for definition, detail in ranked:
        for node in detail.nodes:
            if node.lab_id is None or node.state is not LearningNodeState.AVAILABLE:
                continue
            return _node_recommendation(detail.summary.id, node)
        locked = next(
            (node for node in definition.nodes if node.type == "composite" and node.requirements),
            None,
        )
        if locked is not None:
            missing = next(
                (
                    requirement
                    for requirement in locked.requirements
                    if not _met(requirement, facts)
                ),
                None,
            )
            if missing is not None:
                action = (
                    RecommendationAction.PRACTICE_VARIANT
                    if missing.kind == "variant_completed"
                    else RecommendationAction.START_BASELINE
                )
                return LearningRecommendation(
                    action=action,
                    title=f"完成解锁前置：{missing.lab_id}",
                    reason=_requirement_reason(missing),
                    path_id=detail.summary.id,
                    node_id=locked.id,
                    lab_id=missing.lab_id,
                )

    review = next(
        (
            (detail, node)
            for detail in details
            for node in detail.nodes
            if node.lab_id is not None and node.state is LearningNodeState.REVIEW_RECOMMENDED
        ),
        None,
    )
    if review is not None:
        detail, node = review
        return LearningRecommendation(
            action=RecommendationAction.REVIEW,
            title=f"复习：{node.title}",
            reason="最近一次练习记录表明该知识点适合再次巩固。",
            path_id=detail.summary.id,
            node_id=node.id,
            lab_id=node.lab_id,
        )
    return LearningRecommendation(
        action=RecommendationAction.COMPLETE,
        title="专题学习已完成",
        reason="当前四条路径的可执行节点均已完成，可按症状索引自主复习。",
    )


def derive_outcome(
    definition: LearningPathDefinition,
    facts: LearningFacts,
) -> LearningPathOutcome:
    lab_ids = tuple(dict.fromkeys(node.lab_id for node in definition.nodes if node.lab_id))
    selected = tuple(facts.labs.get(lab_id, LabLearningFacts(lab_id=lab_id)) for lab_id in lab_ids)
    composites = tuple(node for node in definition.nodes if node.type == "composite")
    times = tuple(fact.first_completed_at for fact in selected if fact.first_completed_at)
    practiced = tuple(fact.last_practiced_at for fact in selected if fact.last_practiced_at)
    return LearningPathOutcome(
        path_id=definition.metadata.id,
        path_name=definition.metadata.name,
        lab_family_count=len(selected),
        baseline_completed_count=sum(fact.baseline_completed for fact in selected),
        variant_total=sum(fact.variant_total for fact in selected),
        variant_completed=sum(fact.variant_completed for fact in selected),
        composite_total=len(composites),
        composite_completed=sum(
            bool(
                facts.labs.get(
                    node.lab_id or "", LabLearningFacts(lab_id="missing")
                ).baseline_completed
            )
            for node in composites
        ),
        hint_request_count=sum(fact.hint_request_count for fact in selected),
        manual_verification_count=sum(fact.manual_verification_count for fact in selected),
        first_completed_at=min(times) if times else None,
        last_practiced_at=max(practiced) if practiced else None,
        revealed_scenarios=tuple(
            dict.fromkeys(name for fact in selected for name in fact.revealed_scenarios)
        ),
        review_lab_ids=tuple(fact.lab_id for fact in selected if _review_recommended(fact)),
    )


def render_outcome_markdown(outcome: LearningPathOutcome) -> str:
    """Render a bounded, sanitized topic outcome without validation internals."""
    first_completed = (
        outcome.first_completed_at.isoformat() if outcome.first_completed_at else "—"
    )
    last_practiced = outcome.last_practiced_at.isoformat() if outcome.last_practiced_at else "—"
    lines = [
        "# KubeLab 专题学习成果",
        "",
        f"- 专题：{_markdown(outcome.path_name)} (`{outcome.path_id}`)",
        f"- 基线完成：{outcome.baseline_completed_count} / {outcome.lab_family_count}",
        f"- 固定变体完成：{outcome.variant_completed} / {outcome.variant_total}",
        f"- 综合实验完成：{outcome.composite_completed} / {outcome.composite_total}",
        f"- 提示请求：{outcome.hint_request_count}",
        f"- 手动验证：{outcome.manual_verification_count}",
        f"- 首次完成：{first_completed}",
        f"- 最近练习：{last_practiced}",
        "",
        "## 已揭示场景",
        "",
    ]
    lines.extend(f"- {_markdown(name)}" for name in outcome.revealed_scenarios)
    if not outcome.revealed_scenarios:
        lines.append("- 暂无")
    lines.extend(("", "## 建议复习", ""))
    lines.extend(f"- `{lab_id}`" for lab_id in outcome.review_lab_ids)
    if not outcome.review_lab_ids:
        lines.append("- 当前没有确定性复习建议")
    return "\n".join(lines)[:32_000].rstrip() + "\n"


def _validate_catalog_references(
    catalog: LearningPathCatalogDefinition, lab_ids: frozenset[str]
) -> LearningPathRegistryError | None:
    for card_index, card in enumerate(catalog.knowledge_cards):
        if card.lab_id not in lab_ids:
            return _reference_error(f"knowledgeCards.{card_index}.labId")
    card_ids = {card.lab_id for card in catalog.knowledge_cards}
    for path_index, path in enumerate(catalog.paths):
        node_ids = {node.id for node in path.nodes}
        graph = {node.id: node.depends_on for node in path.nodes}
        for node_index, node in enumerate(path.nodes):
            if node.lab_id is not None and node.lab_id not in lab_ids:
                return _reference_error(f"paths.{path_index}.nodes.{node_index}.labId")
            if node.lab_id is not None and node.lab_id not in card_ids:
                return _reference_error(f"paths.{path_index}.nodes.{node_index}.labId")
            for dependency in node.depends_on:
                if dependency not in node_ids:
                    return LearningPathRegistryError(
                        code=LearningPathErrorCode.NODE_REFERENCE_INVALID,
                        message="A learning path node references an unknown dependency.",
                        field_path=f"paths.{path_index}.nodes.{node_index}.dependsOn",
                    )
            for requirement_index, requirement in enumerate(node.requirements):
                if requirement.lab_id not in lab_ids:
                    return _reference_error(
                        f"paths.{path_index}.nodes.{node_index}.requirements."
                        f"{requirement_index}.labId"
                    )
        if _has_cycle(graph):
            return LearningPathRegistryError(
                code=LearningPathErrorCode.DEPENDENCY_CYCLE,
                message="A learning path contains a dependency cycle.",
                field_path=f"paths.{path_index}.nodes",
            )
    for symptom_index, symptom in enumerate(catalog.symptoms):
        if any(lab_id not in lab_ids for lab_id in symptom.lab_ids):
            return _reference_error(f"symptoms.{symptom_index}.labIds")
    return None


def _has_cycle(graph: Mapping[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _registry_failure(
    code: LearningPathErrorCode, message: str, *, field_path: str | None = None
) -> LearningPathRegistrySnapshot:
    return LearningPathRegistrySnapshot(
        catalog=None,
        errors=(LearningPathRegistryError(code=code, message=message, field_path=field_path),),
    )


def _reference_error(field_path: str) -> LearningPathRegistryError:
    return LearningPathRegistryError(
        code=LearningPathErrorCode.LAB_REFERENCE_INVALID,
        message="The learning path catalog references an unknown lab.",
        field_path=field_path,
    )


def _validation_field_path(error: Exception) -> str | None:
    if isinstance(error, ValidationError) and error.errors():
        return ".".join(str(item) for item in error.errors()[0]["loc"])
    return None


def _node_completed(node: LearningPathNodeDefinition, fact: LabLearningFacts | None) -> bool:
    if node.type == "concept":
        return True
    if node.type == "retrospective":
        return False
    if fact is None:
        return False
    if node.type in {"baseline", "composite"}:
        return fact.baseline_completed
    return fact.variant_completed > 0


def _node_active(node: LearningPathNodeDefinition, facts: LearningFacts) -> bool:
    if node.lab_id is None or node.lab_id != facts.active_lab_id:
        return False
    if node.type == "variant":
        return facts.active_variant_id not in {None, "baseline"}
    return node.type in {"baseline", "composite"} and facts.active_variant_id == "baseline"


def _lock_reasons(
    node: LearningPathNodeDefinition,
    path: LearningPathDefinition,
    completed: Mapping[str, bool],
    facts: LearningFacts,
) -> tuple[str, ...]:
    by_id = {item.id: item for item in path.nodes}
    reasons = [f"先完成「{by_id[item].title}」" for item in node.depends_on if not completed[item]]
    reasons.extend(
        _requirement_reason(requirement)
        for requirement in node.requirements
        if not _met(requirement, facts)
    )
    return tuple(reasons)


def _met(requirement: LearningRequirement, facts: LearningFacts) -> bool:
    fact = facts.labs.get(requirement.lab_id)
    if fact is None:
        return False
    if requirement.kind == "baseline_completed":
        return fact.baseline_completed
    return fact.variant_completed >= (requirement.minimum_variant_count or 1)


def _requirement_reason(requirement: LearningRequirement) -> str:
    if requirement.kind == "baseline_completed":
        return f"先完成 {requirement.lab_id} 原始基线"
    return f"先完成 {requirement.lab_id} 至少 {requirement.minimum_variant_count or 1} 个固定变体"


def _review_recommended(fact: LabLearningFacts) -> bool:
    if not fact.baseline_completed:
        return False
    return bool(
        (fact.variant_total > 0 and fact.variant_completed == 0)
        or fact.latest_attempt_passed is False
        or fact.latest_unlocked_hint_count >= 3
        or fact.latest_manual_verification_count >= 4
    )


def _node_recommendation(path_id: str, node: LearningPathNode) -> LearningRecommendation:
    action = {
        "baseline": RecommendationAction.START_BASELINE,
        "variant": RecommendationAction.PRACTICE_VARIANT,
        "composite": RecommendationAction.START_COMPOSITE,
    }[node.type]
    reason = {
        "baseline": "这是当前专题中下一个尚未完成的原始基线。",
        "variant": "基线已经通过，下一步用固定变体检验知识迁移。",
        "composite": "所有硬性前置能力已经满足，可以进入双根因综合排障。",
    }[node.type]
    return LearningRecommendation(
        action=action,
        title=node.title,
        reason=reason,
        path_id=path_id,
        node_id=node.id,
        lab_id=node.lab_id,
    )


def _markdown(value: str) -> str:
    redacted = str(redact_json(value))[:2000]
    escaped = html.escape(redacted, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "#"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped.replace("\r", "").replace("\n", " ").strip()


__all__ = [
    "AfterKnowledgeCard",
    "BeforeKnowledgeCard",
    "LabKnowledgeCard",
    "LabLearningFacts",
    "LearningFacts",
    "LearningNodeState",
    "LearningPathCatalogDefinition",
    "LearningPathCatalogReport",
    "LearningPathDefinition",
    "LearningPathDetail",
    "LearningPathErrorCode",
    "LearningPathOutcome",
    "LearningPathRegistry",
    "LearningPathRegistryError",
    "LearningPathRegistrySnapshot",
    "LearningPathSummary",
    "LearningRecommendation",
    "RecommendationAction",
    "SymptomCatalog",
    "SymptomDefinition",
    "derive_outcome",
    "evaluate_path",
    "recommend_next",
    "render_outcome_markdown",
]
