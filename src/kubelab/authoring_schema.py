"""Strict declarative contracts consumed only by the M8 author toolchain."""

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

from kubelab.lab_schema import CheckId, KubernetesName, NonEmptyText

AUTHORING_API_VERSION = "kubelab.io/v1alpha1"
AUTHORING_KIND = "LabAuthoringContract"

RelativePath = Annotated[
    str,
    StringConstraints(
        pattern=r"^[^\\\x00]{1,240}$",
        min_length=1,
        max_length=240,
    ),
]
JsonPointer = Annotated[
    str,
    StringConstraints(pattern=r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$", max_length=512),
]


class AuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class AuthorResource(AuthoringModel):
    api_version: NonEmptyText = Field(alias="apiVersion")
    kind: NonEmptyText
    name: KubernetesName


class AllowedChange(AuthoringModel):
    resource: AuthorResource
    operation: Literal["modify", "create", "recreate"]
    paths: tuple[JsonPointer, ...] = ()

    @field_validator("paths")
    @classmethod
    def paths_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed change paths must be unique")
        return value

    @model_validator(mode="after")
    def operation_matches_paths(self) -> AllowedChange:
        if self.operation in {"modify", "recreate"} and not self.paths:
            raise ValueError(f"{self.operation} requires at least one JSON Pointer")
        if self.operation == "create" and self.paths:
            raise ValueError("create must not declare field paths")
        return self


class RepairPlan(AuthoringModel):
    manifest: RelativePath
    allowed_changes: tuple[AllowedChange, ...] = Field(alias="allowedChanges", min_length=1)

    @field_validator("manifest")
    @classmethod
    def manifest_is_relative(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("repair manifest must be a normalized relative path")
        return value

    @field_validator("allowed_changes")
    @classmethod
    def resources_are_unique(cls, value: tuple[AllowedChange, ...]) -> tuple[AllowedChange, ...]:
        identities = {
            (item.resource.api_version, item.resource.kind, item.resource.name) for item in value
        }
        if len(identities) != len(value):
            raise ValueError("each repair resource may be declared only once")
        return value


class FakeContainer(AuthoringModel):
    name: KubernetesName
    image: NonEmptyText
    ready: bool
    restart_count: int = Field(alias="restartCount", ge=0, le=10_000)
    state: Literal["running", "waiting", "terminated"] | None = None
    reason: Annotated[str, StringConstraints(max_length=120)] | None = None


class FakePod(AuthoringModel):
    name: KubernetesName
    phase: Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"] | None
    ready: bool
    restart_count: int = Field(alias="restartCount", ge=0, le=10_000)
    containers: tuple[FakeContainer, ...] = Field(min_length=1, max_length=10)


class ResourceExistsObservation(AuthoringModel):
    type: Literal["resource_exists"]
    exists: bool


class PodStatusObservation(AuthoringModel):
    type: Literal["pod_status"]
    pods: tuple[FakePod, ...] = Field(max_length=20)


class DeploymentAvailableObservation(AuthoringModel):
    type: Literal["deployment_available"]
    available_replicas: int | None = Field(alias="availableReplicas", ge=0, le=20)


class ServiceEndpointObservation(AuthoringModel):
    type: Literal["service_endpoint_count"]
    count: int | None = Field(ge=0, le=100)


class ContainerImageObservation(AuthoringModel):
    type: Literal["container_image"]
    image: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None


class ConfigValueObservation(AuthoringModel):
    type: Literal["config_value"]
    resource_exists: bool = Field(alias="resourceExists")
    key_exists: bool = Field(alias="keyExists")
    matched: bool
    valid_encoding: bool = Field(alias="validEncoding", default=True)


class PvcStatusObservation(AuthoringModel):
    type: Literal["pvc_status"]
    phase: Literal["Pending", "Bound", "Lost"] | None


class HttpResponseObservation(AuthoringModel):
    type: Literal["http_response"]
    target_available: bool = Field(alias="targetAvailable", default=True)
    status_code: int | None = Field(alias="statusCode", default=None, ge=100, le=599)
    exit_code: int | None = Field(alias="exitCode", default=None, ge=0, le=255)
    infrastructure_error: bool = Field(alias="infrastructureError", default=False)
    timed_out: bool = Field(alias="timedOut", default=False)
    cleanup_warning: bool = Field(alias="cleanupWarning", default=False)


class DnsResolutionObservation(AuthoringModel):
    type: Literal["dns_resolution"]
    resolved: bool
    infrastructure_error: bool = Field(alias="infrastructureError", default=False)
    timed_out: bool = Field(alias="timedOut", default=False)
    cleanup_warning: bool = Field(alias="cleanupWarning", default=False)


FakeObservation = Annotated[
    ResourceExistsObservation
    | PodStatusObservation
    | DeploymentAvailableObservation
    | ServiceEndpointObservation
    | ContainerImageObservation
    | ConfigValueObservation
    | PvcStatusObservation
    | HttpResponseObservation
    | DnsResolutionObservation,
    Field(discriminator="type"),
]


class AuthoringState(AuthoringModel):
    observations: dict[CheckId, FakeObservation] = Field(min_length=1, max_length=100)


class AuthoringStates(AuthoringModel):
    faulted: AuthoringState
    repaired: AuthoringState
    reset: Literal["faulted"]
    first_repair: AuthoringState | None = Field(alias="firstRepair", default=None)


class AuthoringRepairs(AuthoringModel):
    full: RepairPlan
    first: RepairPlan | None = None


class LabAuthoringContract(AuthoringModel):
    api_version: Literal["kubelab.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LabAuthoringContract"]
    scenario_type: Literal["baseline", "variant", "composite"] = Field(alias="scenarioType")
    states: AuthoringStates
    repairs: AuthoringRepairs

    @model_validator(mode="after")
    def composite_stages_are_consistent(self) -> LabAuthoringContract:
        composite = self.scenario_type == "composite"
        if composite != (self.states.first_repair is not None):
            raise ValueError("only composite scenarios require states.firstRepair")
        if composite != (self.repairs.first is not None):
            raise ValueError("only composite scenarios require repairs.first")
        return self


__all__ = [
    "AUTHORING_API_VERSION",
    "AUTHORING_KIND",
    "AllowedChange",
    "AuthorResource",
    "AuthoringState",
    "ContainerImageObservation",
    "ConfigValueObservation",
    "DeploymentAvailableObservation",
    "DnsResolutionObservation",
    "FakeObservation",
    "FakePod",
    "HttpResponseObservation",
    "LabAuthoringContract",
    "PodStatusObservation",
    "PvcStatusObservation",
    "RepairPlan",
    "ResourceExistsObservation",
    "ServiceEndpointObservation",
]
