"""Strict M9 package metadata and portable index contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from kubelab.package_schema import LabPackageDefinition, LabPackageIndex


def package_definition() -> dict[str, Any]:
    return {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "LabPackage",
        "metadata": {
            "labId": "lab-local-networking",
            "version": "1.2.3-rc.1",
            "publisherId": "local-author",
            "publisherName": "Local Author",
        },
        "spec": {"requiresKubelab": ">=0.6.0a0,<0.7.0"},
    }


@pytest.mark.parametrize(
    "version",
    ["1.2", "01.2.3", "1.2.3+build.1", "1.2.3-01", "v1.2.3"],
)
def test_package_metadata_rejects_non_semver_or_build_metadata(version: str) -> None:
    candidate = deepcopy(package_definition())
    candidate["metadata"]["version"] = version

    with pytest.raises(ValidationError):
        LabPackageDefinition.model_validate(candidate)


def test_package_metadata_rejects_invalid_compatibility_specifier() -> None:
    candidate = package_definition()
    candidate["spec"]["requiresKubelab"] = "next release"

    with pytest.raises(ValidationError):
        LabPackageDefinition.model_validate(candidate)


def test_v2_index_is_strict_and_bounded() -> None:
    candidate = {
        "formatVersion": 2,
        "labId": "lab-local-networking",
        "package": {
            "version": "1.2.3",
            "publisherId": "local-author",
            "publisherName": "Local Author",
            "requiresKubelab": ">=0.6.0a0,<0.7.0",
        },
        "schemaVersions": {
            "lab": "kubelab.io/v1alpha1",
            "authoring": "kubelab.io/v1alpha1",
            "package": "kubelab.io/v1alpha1",
        },
        "scenarios": ["lab-local-networking"],
        "files": [
            {
                "path": "labs/lab-local-networking/lab.yaml",
                "size": 12,
                "sha256": "0" * 64,
            }
        ],
    }

    parsed = LabPackageIndex.model_validate(candidate)

    assert parsed.format_version == 2
    candidate["unexpected"] = True
    with pytest.raises(ValidationError):
        LabPackageIndex.model_validate(candidate)
