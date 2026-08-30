"""Pydantic models for the declarative KubeLab v1alpha1 format."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

LAB_API_VERSION = "kubelab.io/v1alpha1"
LAB_KIND = "Lab"
LAB_VARIANT_KIND = "LabVariant"

Slug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", min_length=1, max_length=63),
]
LabId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{2,39}$", min_length=3, max_length=40),
]
KubernetesName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$",
        min_length=1,
        max_length=253,
    ),
]
CheckId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$", min_length=2, max_length=63),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class LabModel(BaseModel):
    """Strict immutable base for every public lab DTO."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LabMetadata(LabModel):
    """Human-facing identity and catalogue data for one lab."""

    id: LabId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: NonEmptyText
    difficulty: Literal["beginner", "intermediate", "advanced"]
    duration_minutes: int = Field(alias="durationMinutes", ge=5, le=180)
    category: Slug
    tags: tuple[Slug, ...] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def tags_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("tags must be unique")
        return value


class LabRequirements(LabModel):
    """Minimum local cluster capabilities declared by a lab."""

    kubernetes: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    minimum_cpu: int = Field(alias="minimumCpu", ge=1, le=64)
    minimum_memory_mib: int = Field(alias="minimumMemoryMiB", ge=128, le=131_072)
    addons: tuple[Slug, ...]

    @field_validator("addons")
    @classmethod
    def addons_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("addons must be unique")
        return value


class LabEnvironment(LabModel):
    """Declarative namespace and manifest inputs for the faulty environment."""

    namespace: Annotated[
        str,
        StringConstraints(
            pattern=r"^kubelab-[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$",
            min_length=10,
            max_length=63,
        ),
    ]
    manifests: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(min_length=1)
    provision_timeout_seconds: int = Field(alias="provisionTimeoutSeconds", ge=10, le=300)

    @field_validator("manifests")
    @classmethod
    def manifests_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("manifest paths must be unique")
        return value


class LabTask(LabModel):
    """User-facing task and completion copy."""

    description: NonEmptyText
    completion_description: NonEmptyText = Field(alias="completionDescription")
    success_message: NonEmptyText = Field(alias="successMessage")


class CheckBase(LabModel):
    """Fields shared by all declarative checks."""

    id: CheckId
    timeout_seconds: int = Field(alias="timeoutSeconds", ge=1, le=120)
    unmet_message: NonEmptyText = Field(alias="unmetMessage")


class ResourceExistsCheck(CheckBase):
    type: Literal["resource_exists"]
    api_version: NonEmptyText = Field(alias="apiVersion")
    kind: NonEmptyText
    name: KubernetesName


class PodStatusCheck(CheckBase):
    type: Literal["pod_status"]
    selector: dict[Slug, NonEmptyText] = Field(min_length=1)
    expected_phase: Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"] = Field(
        alias="expectedPhase"
    )
    minimum_count: int = Field(alias="minimumCount", ge=1, default=1)
    minimum_ready: int | None = Field(alias="minimumReady", ge=0, default=None)
    stable_seconds: int | None = Field(alias="stableSeconds", ge=0, le=120, default=None)
    ready: bool | None = None
    container_name: KubernetesName | None = Field(alias="containerName", default=None)
    expected_waiting_reasons: tuple[NonEmptyText, ...] | None = Field(
        alias="expectedWaitingReasons", min_length=1, default=None
    )
    minimum_restart_count: int | None = Field(alias="minimumRestartCount", ge=0, default=None)
    maximum_restart_count: int | None = Field(alias="maximumRestartCount", ge=0, default=None)

    @field_validator("expected_waiting_reasons")
    @classmethod
    def waiting_reasons_are_unique(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("expectedWaitingReasons must be unique")
        return value

    @model_validator(mode="after")
    def readiness_is_consistent(self) -> PodStatusCheck:
        if self.minimum_ready is not None and self.minimum_ready > self.minimum_count:
            raise ValueError("minimumReady cannot exceed minimumCount")
        if self.stable_seconds is not None and self.minimum_ready is None:
            raise ValueError("stableSeconds requires minimumReady")
        container_constraints = (
            self.expected_waiting_reasons,
            self.minimum_restart_count,
            self.maximum_restart_count,
        )
        if (
            any(value is not None for value in container_constraints)
            and self.container_name is None
        ):
            raise ValueError("container status constraints require containerName")
        if (
            self.minimum_restart_count is not None
            and self.maximum_restart_count is not None
            and self.minimum_restart_count > self.maximum_restart_count
        ):
            raise ValueError("minimumRestartCount cannot exceed maximumRestartCount")
        return self


class DeploymentAvailableCheck(CheckBase):
    type: Literal["deployment_available"]
    name: KubernetesName
    minimum_replicas: int = Field(alias="minimumReplicas", ge=1, le=20)


class ServiceEndpointCountCheck(CheckBase):
    type: Literal["service_endpoint_count"]
    name: KubernetesName
    minimum: int | None = Field(ge=0, default=None)
    maximum: int | None = Field(ge=0, default=None)
    exactly: int | None = Field(ge=0, default=None)

    @model_validator(mode="after")
    def endpoint_bounds_are_consistent(self) -> ServiceEndpointCountCheck:
        bounds = (self.minimum, self.maximum, self.exactly)
        if all(value is None for value in bounds):
            raise ValueError("one endpoint count constraint is required")
        if self.exactly is not None and (self.minimum is not None or self.maximum is not None):
            raise ValueError("exactly cannot be combined with minimum or maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        return self


class ContainerImageCheck(CheckBase):
    type: Literal["container_image"]
    workload_kind: Literal["Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"] = (
        Field(alias="workloadKind")
    )
    workload_name: KubernetesName = Field(alias="workloadName")
    container: KubernetesName
    expected_image: NonEmptyText = Field(alias="expectedImage")


class ConfigValueCheck(CheckBase):
    type: Literal["config_value"]
    source_kind: Literal["ConfigMap", "Secret"] = Field(alias="sourceKind")
    source_name: KubernetesName = Field(alias="sourceName")
    key: NonEmptyText
    expected_value: str = Field(alias="expectedValue")


class PvcStatusCheck(CheckBase):
    type: Literal["pvc_status"]
    name: KubernetesName
    expected_phase: Literal["Pending", "Bound", "Lost"] = Field(alias="expectedPhase")


class HttpTarget(LabModel):
    """Cluster-internal HTTP target; raw URLs are deliberately not supported."""

    mode: Literal["service", "ingress"]
    name: KubernetesName
    port: int = Field(ge=1, le=65_535)
    path: Annotated[str, StringConstraints(pattern=r"^/", min_length=1, max_length=2048)] = "/"
    scheme: Literal["http", "https"] = "http"


class HttpResponseCheck(CheckBase):
    type: Literal["http_response"]
    target: HttpTarget
    expected_status: int = Field(alias="expectedStatus", ge=100, le=599)


class DnsResolutionCheck(CheckBase):
    """Restricted Kubernetes service DNS check; arbitrary hostnames are forbidden."""

    type: Literal["dns_resolution"]
    service: KubernetesName
    pod: KubernetesName | None = None
    expected_resolved: bool = Field(alias="expectedResolved")


CheckDefinition = Annotated[
    ResourceExistsCheck
    | PodStatusCheck
    | DeploymentAvailableCheck
    | ServiceEndpointCountCheck
    | ContainerImageCheck
    | ConfigValueCheck
    | PvcStatusCheck
    | HttpResponseCheck
    | DnsResolutionCheck,
    Field(discriminator="type"),
]


class LabHint(LabModel):
    level: int = Field(ge=1, le=3)
    content: NonEmptyText


class LabCleanup(LabModel):
    """The only cleanup action permitted in v1alpha1."""

    delete_namespace: Literal[True] = Field(alias="deleteNamespace")


class LabInterview(LabModel):
    questions: tuple[NonEmptyText, ...] = Field(min_length=1)


class LabVariantMetadata(LabModel):
    """Opaque runtime identity plus post-pass learning copy for one fixed variant."""

    id: Annotated[
        str,
        StringConstraints(pattern=r"^variant-[a-z0-9][a-z0-9-]{0,53}$", max_length=63),
    ]
    sequence: int = Field(ge=1, le=20)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    description: NonEmptyText


class LabVariantEnvironment(LabModel):
    """Variant manifests; namespace and requirements are inherited from the parent Lab."""

    manifests: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(min_length=1)
    provision_timeout_seconds: int = Field(alias="provisionTimeoutSeconds", ge=10, le=300)

    @field_validator("manifests")
    @classmethod
    def manifests_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("manifest paths must be unique")
        return value


class LabVariantReveal(LabModel):
    """Learning outcome disclosed only after this scenario passes."""

    key_evidence: NonEmptyText = Field(alias="keyEvidence")
    root_cause: NonEmptyText = Field(alias="rootCause")
    resolution: NonEmptyText
    prevention: NonEmptyText


class LabVariantDefinition(LabModel):
    """Strict fixed variant definition scoped by its parent lab directory."""

    api_version: Literal["kubelab.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LabVariant"]
    metadata: LabVariantMetadata
    environment: LabVariantEnvironment
    task: LabTask
    initial_checks: tuple[CheckDefinition, ...] = Field(alias="initialChecks", min_length=1)
    success_checks: tuple[CheckDefinition, ...] = Field(alias="successChecks", min_length=1)
    hints: tuple[LabHint, ...] = Field(min_length=3, max_length=3)
    reveal: LabVariantReveal

    @model_validator(mode="after")
    def cross_field_invariants(self) -> LabVariantDefinition:
        check_ids = [check.id for check in (*self.initial_checks, *self.success_checks)]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("check IDs must be unique across the entire variant")
        if [hint.level for hint in self.hints] != [1, 2, 3]:
            raise ValueError("variant hint levels must be exactly 1, 2, 3")
        return self


class LabDefinition(LabModel):
    """Complete kubelab.io/v1alpha1 experiment definition."""

    api_version: Literal["kubelab.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["Lab"]
    metadata: LabMetadata
    requirements: LabRequirements
    environment: LabEnvironment
    task: LabTask
    initial_checks: tuple[CheckDefinition, ...] = Field(alias="initialChecks", min_length=1)
    success_checks: tuple[CheckDefinition, ...] = Field(alias="successChecks", min_length=1)
    hints: tuple[LabHint, ...] = Field(min_length=1, max_length=3)
    cleanup: LabCleanup
    interview: LabInterview

    @model_validator(mode="after")
    def cross_field_invariants(self) -> LabDefinition:
        check_ids = [check.id for check in (*self.initial_checks, *self.success_checks)]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("check IDs must be unique across the entire lab")

        levels = [hint.level for hint in self.hints]
        if levels != list(range(1, len(levels) + 1)):
            raise ValueError("hint levels must start at 1 and be continuous")
        return self


__all__ = [
    "CheckDefinition",
    "DnsResolutionCheck",
    "LabDefinition",
    "LabMetadata",
    "LabVariantDefinition",
    "LabVariantReveal",
]
