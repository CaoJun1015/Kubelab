"""Contract tests for the kubelab.io/v1alpha1 Pydantic models."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kubelab.lab_schema import LabDefinition
from kubelab.schema_export import render_lab_json_schema


def complete_lab() -> dict[str, Any]:
    return {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "Lab",
        "metadata": {
            "id": "schema-lab",
            "name": "Schema lab",
            "description": "Exercises every v1alpha1 check model.",
            "difficulty": "beginner",
            "durationMinutes": 20,
            "category": "networking",
            "tags": ["pod", "service"],
        },
        "requirements": {
            "kubernetes": ">=1.28",
            "minimumCpu": 2,
            "minimumMemoryMiB": 2048,
            "addons": [],
        },
        "environment": {
            "namespace": "kubelab-schema-lab",
            "manifests": ["manifests/resources.yaml"],
            "provisionTimeoutSeconds": 120,
        },
        "task": {
            "description": "Find the fault.",
            "completionDescription": "Restore the workload.",
            "successMessage": "The workload is restored.",
        },
        "initialChecks": [
            {
                "id": "resource-present",
                "type": "resource_exists",
                "apiVersion": "v1",
                "kind": "Service",
                "name": "web",
                "timeoutSeconds": 10,
                "unmetMessage": "Service is missing.",
            },
            {
                "id": "pods-running",
                "type": "pod_status",
                "selector": {"app": "web"},
                "expectedPhase": "Running",
                "minimumCount": 2,
                "minimumReady": 1,
                "stableSeconds": 5,
                "timeoutSeconds": 20,
                "unmetMessage": "Pods are not ready.",
            },
            {
                "id": "deployment-ready",
                "type": "deployment_available",
                "name": "web",
                "minimumReplicas": 1,
                "timeoutSeconds": 20,
                "unmetMessage": "Deployment is unavailable.",
            },
            {
                "id": "endpoint-empty",
                "type": "service_endpoint_count",
                "name": "web",
                "exactly": 0,
                "timeoutSeconds": 20,
                "unmetMessage": "Endpoints are not empty.",
            },
        ],
        "successChecks": [
            {
                "id": "image-fixed",
                "type": "container_image",
                "workloadKind": "Deployment",
                "workloadName": "web",
                "container": "web",
                "expectedImage": "nginx:1.27",
                "timeoutSeconds": 20,
                "unmetMessage": "Image is incorrect.",
            },
            {
                "id": "config-fixed",
                "type": "config_value",
                "sourceKind": "ConfigMap",
                "sourceName": "web",
                "key": "MODE",
                "expectedValue": "production",
                "timeoutSeconds": 20,
                "unmetMessage": "Configuration is incorrect.",
            },
            {
                "id": "pvc-bound",
                "type": "pvc_status",
                "name": "data",
                "expectedPhase": "Bound",
                "timeoutSeconds": 20,
                "unmetMessage": "PVC is not bound.",
            },
            {
                "id": "http-ok",
                "type": "http_response",
                "target": {
                    "mode": "service",
                    "name": "web",
                    "port": 80,
                    "path": "/healthz",
                },
                "expectedStatus": 200,
                "timeoutSeconds": 20,
                "unmetMessage": "HTTP response is unhealthy.",
            },
        ],
        "hints": [
            {"level": 1, "content": "Inspect the resources."},
            {"level": 2, "content": "Compare desired and actual state."},
        ],
        "cleanup": {"deleteNamespace": True},
        "interview": {"questions": ["What did you inspect first?"]},
    }


def test_complete_schema_accepts_all_eight_check_types() -> None:
    lab = LabDefinition.model_validate(complete_lab())

    assert lab.api_version == "kubelab.io/v1alpha1"
    assert {check.type for check in (*lab.initial_checks, *lab.success_checks)} == {
        "resource_exists",
        "pod_status",
        "deployment_available",
        "service_endpoint_count",
        "container_image",
        "config_value",
        "pvc_status",
        "http_response",
    }
    assert lab.model_dump(by_alias=True)["cleanup"] == {"deleteNamespace": True}


@pytest.mark.parametrize(
    ("mutation", "path_fragment"),
    [
        (lambda lab: lab.update(apiVersion="kubelab.io/v1"), "apiVersion"),
        (lambda lab: lab.update(kind="Experiment"), "kind"),
        (lambda lab: lab.update(initialChecks=[]), "initialChecks"),
        (lambda lab: lab.update(successChecks=[]), "successChecks"),
    ],
)
def test_fixed_values_and_required_check_lists_are_enforced(
    mutation: Any, path_fragment: str
) -> None:
    data = complete_lab()
    mutation(data)

    with pytest.raises(ValidationError) as caught:
        LabDefinition.model_validate(data)

    assert path_fragment in str(caught.value)


def test_unknown_fields_and_command_fields_are_rejected() -> None:
    data = complete_lab()
    data["unexpected"] = True
    data["cleanup"]["shell"] = "rm -rf /"

    with pytest.raises(ValidationError) as caught:
        LabDefinition.model_validate(data)

    message = str(caught.value)
    assert "unexpected" in message
    assert "shell" in message


def test_check_ids_are_unique_across_both_lists() -> None:
    data = complete_lab()
    data["successChecks"][0]["id"] = data["initialChecks"][0]["id"]

    with pytest.raises(ValidationError, match="check IDs must be unique"):
        LabDefinition.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["hints"].__setitem__(1, {"level": 3, "content": "Gap"}),
        lambda data: data["initialChecks"][1].update(minimumReady=3),
        lambda data: data["initialChecks"][1].update(stableSeconds=5, minimumReady=None),
        lambda data: data["initialChecks"][3].update(minimum=1),
        lambda data: data["initialChecks"][3].update(exactly=None, minimum=3, maximum=2),
    ],
)
def test_cross_field_invariants_are_enforced(change: Any) -> None:
    data = deepcopy(complete_lab())
    change(data)

    with pytest.raises(ValidationError):
        LabDefinition.model_validate(data)


def test_pod_status_accepts_precise_container_failure_contract() -> None:
    data = complete_lab()
    data["initialChecks"][1].update(
        ready=False,
        containerName="web",
        expectedWaitingReasons=["ErrImagePull", "ImagePullBackOff"],
        minimumRestartCount=1,
        maximumRestartCount=3,
    )

    lab = LabDefinition.model_validate(data)
    check = lab.initial_checks[1]

    assert check.type == "pod_status"
    assert check.container_name == "web"
    assert check.expected_waiting_reasons == ("ErrImagePull", "ImagePullBackOff")


@pytest.mark.parametrize(
    "change",
    [
        lambda check: check.update(expectedWaitingReasons=["CrashLoopBackOff"]),
        lambda check: check.update(minimumRestartCount=1),
        lambda check: check.update(maximumRestartCount=2),
        lambda check: check.update(
            containerName="web", minimumRestartCount=4, maximumRestartCount=3
        ),
        lambda check: check.update(
            containerName="web",
            expectedWaitingReasons=["CrashLoopBackOff", "CrashLoopBackOff"],
        ),
    ],
)
def test_pod_container_status_constraints_are_consistent(change: Any) -> None:
    data = complete_lab()
    change(data["initialChecks"][1])

    with pytest.raises(ValidationError):
        LabDefinition.model_validate(data)


def test_json_schema_is_in_sync_and_deterministic() -> None:
    project_root = Path(__file__).resolve().parents[1]
    committed = (project_root / "schemas" / "lab-v1alpha1.schema.json").read_bytes()
    generated = render_lab_json_schema().encode("utf-8")

    assert committed == generated
    assert generated == render_lab_json_schema().encode("utf-8")
    assert b'"discriminator"' in committed
    assert b'"http_response"' in committed
