"""Strict metadata for versioned local KubeLab experiment packages."""

from __future__ import annotations

from typing import Annotated, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from kubelab.lab_schema import LabId, Slug

PACKAGE_API_VERSION = "kubelab.io/v1alpha1"
PACKAGE_KIND = "LabPackage"
PACKAGE_FORMAT_VERSION = 2

SemVer = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        ),
        min_length=5,
        max_length=64,
    ),
]


class PackageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LabPackageMetadata(PackageModel):
    lab_id: LabId = Field(alias="labId")
    version: SemVer
    publisher_id: Slug = Field(alias="publisherId")
    publisher_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ] = Field(alias="publisherName")

    @field_validator("version")
    @classmethod
    def numeric_prerelease_identifiers_have_no_leading_zeroes(cls, value: str) -> str:
        prerelease = value.partition("-")[2]
        if prerelease and any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in prerelease.split(".")
        ):
            raise ValueError("numeric SemVer prerelease identifiers cannot have leading zeroes")
        return value


class LabPackageSpec(PackageModel):
    requires_kubelab: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ] = Field(alias="requiresKubelab")

    @field_validator("requires_kubelab")
    @classmethod
    def requirement_is_pep440(cls, value: str) -> str:
        try:
            parsed = SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError("requiresKubelab must be a PEP 440 specifier") from exc
        if not str(parsed):
            raise ValueError("requiresKubelab cannot be empty")
        return value


class LabPackageDefinition(PackageModel):
    api_version: Literal["kubelab.io/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["LabPackage"]
    metadata: LabPackageMetadata
    spec: LabPackageSpec


class LabPackageIndexMetadata(PackageModel):
    version: SemVer
    publisher_id: Slug = Field(alias="publisherId")
    publisher_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ] = Field(alias="publisherName")
    requires_kubelab: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ] = Field(alias="requiresKubelab")

    @field_validator("requires_kubelab")
    @classmethod
    def requirement_is_pep440(cls, value: str) -> str:
        LabPackageSpec(requiresKubelab=value)
        return value


class LabPackageIndexSchemas(PackageModel):
    lab: Literal["kubelab.io/v1alpha1"]
    authoring: Literal["kubelab.io/v1alpha1"]
    package: Literal["kubelab.io/v1alpha1"]


class LabPackageIndexFile(PackageModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    size: int = Field(ge=0, le=512 * 1024)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class LabPackageIndex(PackageModel):
    format_version: Literal[2] = Field(alias="formatVersion")
    lab_id: LabId = Field(alias="labId")
    package: LabPackageIndexMetadata
    schema_versions: LabPackageIndexSchemas = Field(alias="schemaVersions")
    scenarios: tuple[Annotated[str, StringConstraints(min_length=1, max_length=80)], ...]
    files: tuple[LabPackageIndexFile, ...]


def package_index_metadata(definition: LabPackageDefinition) -> dict[str, str]:
    """Project package.yaml metadata into the portable v2 archive index."""
    return {
        "version": definition.metadata.version,
        "publisherId": definition.metadata.publisher_id,
        "publisherName": definition.metadata.publisher_name,
        "requiresKubelab": definition.spec.requires_kubelab,
    }


def package_metadata(lab_id: str) -> dict[str, object]:
    """Return the safe default metadata used by author scaffolds."""
    value: dict[str, object] = {
        "apiVersion": PACKAGE_API_VERSION,
        "kind": PACKAGE_KIND,
        "metadata": {
            "labId": lab_id,
            "version": "0.1.0",
            "publisherId": "local-author",
            "publisherName": "Local Author",
        },
        "spec": {"requiresKubelab": ">=0.6.0a0,<0.7.0"},
    }
    LabPackageDefinition.model_validate(value)
    return value


def package_version_key(value: str) -> Version:
    """Return a deterministic SemVer ordering key without accepting build metadata."""
    return Version(value)


__all__ = [
    "PACKAGE_API_VERSION",
    "PACKAGE_FORMAT_VERSION",
    "PACKAGE_KIND",
    "LabPackageDefinition",
    "LabPackageIndex",
    "LabPackageIndexFile",
    "LabPackageIndexMetadata",
    "LabPackageIndexSchemas",
    "LabPackageMetadata",
    "LabPackageSpec",
    "package_metadata",
    "package_index_metadata",
    "package_version_key",
]
