"""Opt-in WSL integration test for the trusted local minikube boundary."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from kubelab.config import load_config, resolve_kubeconfig_path
from kubelab.context_trust import (
    build_context_trust_service,
    trusted_context_fingerprint,
)
from kubelab.kubernetes_gateway import KubernetesGateway, SessionScope
from kubelab.lab_registry import LabRegistry
from kubelab.lab_schema import HttpTarget

pytestmark = pytest.mark.integration


def test_owned_test_namespace_round_trip_in_trusted_minikube() -> None:
    """Create and safely remove only one randomized KubeLab test Namespace."""
    if os.environ.get("KUBELAB_RUN_INTEGRATION") != "1":
        pytest.skip("Set KUBELAB_RUN_INTEGRATION=1 to use the trusted local minikube cluster")
    if not os.environ.get("WSL_DISTRO_NAME"):
        pytest.skip("The real Kubernetes integration test is supported only inside WSL2")

    trust = build_context_trust_service()
    record = trust.assert_trusted_context()
    fingerprint = trusted_context_fingerprint(record)
    identifier = uuid4()
    scope = SessionScope(
        lab_id="integration",
        session_id=str(identifier),
        namespace=f"kubelab-test-{identifier.hex[:12]}",
        context_fingerprint=fingerprint,
    )
    local_config = load_config()
    gateway = KubernetesGateway.from_kubeconfig(
        kubeconfig_path=resolve_kubeconfig_path(local_config),
        context_name=record.name,
        context_fingerprint=fingerprint,
    )
    try:
        gateway.create_environment(scope)
        assert gateway.namespace_exists(scope) is True
        gateway.assert_namespace_owned(scope)
    finally:
        gateway.delete_environment(scope)
        gateway.close()


def test_service_http_probe_round_trip_in_trusted_minikube(tmp_path: Path) -> None:
    """Deploy nginx, probe its Service internally, and leave no Probe or Namespace."""
    _require_integration_environment()
    trust = build_context_trust_service()
    record = trust.assert_trusted_context()
    fingerprint = trusted_context_fingerprint(record)
    identifier = uuid4()
    scope = SessionScope(
        lab_id="integration-http",
        session_id=str(identifier),
        namespace=f"kubelab-test-{identifier.hex[:12]}",
        context_fingerprint=fingerprint,
    )
    registry = _write_http_lab(tmp_path / "labs", scope.namespace)
    snapshot = registry.scan()
    assert snapshot.errors == ()
    loaded = snapshot.labs[0]
    local_config = load_config()
    gateway = KubernetesGateway.from_kubeconfig(
        kubeconfig_path=resolve_kubeconfig_path(local_config),
        context_name=record.name,
        context_fingerprint=fingerprint,
    )
    try:
        gateway.create_environment(scope)
        gateway.apply_lab(scope, loaded, registry)
        _wait_for_nginx(gateway, scope)
        result = gateway.run_http_probe(
            scope,
            HttpTarget(mode="service", name="web", port=80, path="/"),
            deadline=time.monotonic() + 120,
        )
        if result.infrastructure_error:
            pytest.skip(f"HTTP Probe integration environment error: {result.reason}")
        assert result.exit_code == 0
        assert result.status_code == 200
        assert result.status_code != 404
        assert all(not pod.name.startswith("kubelab-probe-") for pod in gateway.list_pods(scope))
    finally:
        gateway.delete_environment(scope)
        assert gateway.namespace_exists(scope) is False
        gateway.close()


def _require_integration_environment() -> None:
    if os.environ.get("KUBELAB_RUN_INTEGRATION") != "1":
        pytest.skip("Set KUBELAB_RUN_INTEGRATION=1 to use the trusted local minikube cluster")
    if not os.environ.get("WSL_DISTRO_NAME"):
        pytest.skip("The real Kubernetes integration test is supported only inside WSL2")


def _write_http_lab(root: Path, namespace: str) -> LabRegistry:
    lab_dir = root / "integration-http"
    manifest_dir = lab_dir / "manifests"
    manifest_dir.mkdir(parents=True)
    lab: dict[str, Any] = {
        "apiVersion": "kubelab.io/v1alpha1",
        "kind": "Lab",
        "metadata": {
            "id": "integration-http",
            "name": "HTTP integration",
            "description": "Validates the internal Service Probe boundary.",
            "difficulty": "beginner",
            "durationMinutes": 5,
            "category": "networking",
            "tags": ["integration"],
        },
        "requirements": {
            "kubernetes": ">=1.28",
            "minimumCpu": 1,
            "minimumMemoryMiB": 512,
            "addons": [],
        },
        "environment": {
            "namespace": namespace,
            "manifests": ["manifests/resources.yaml"],
            "provisionTimeoutSeconds": 120,
        },
        "task": {
            "description": "Probe nginx.",
            "completionDescription": "Receive HTTP 200.",
            "successMessage": "Probe passed.",
        },
        "initialChecks": [
            {
                "id": "deployment-exists",
                "type": "resource_exists",
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "web",
                "timeoutSeconds": 10,
                "unmetMessage": "Deployment is missing.",
            }
        ],
        "successChecks": [
            {
                "id": "http-ok",
                "type": "http_response",
                "target": {"mode": "service", "name": "web", "port": 80, "path": "/"},
                "expectedStatus": 200,
                "timeoutSeconds": 120,
                "unmetMessage": "HTTP response is unhealthy.",
            }
        ],
        "hints": [{"level": 1, "content": "Inspect the Service endpoints."}],
        "cleanup": {"deleteNamespace": True},
        "interview": {"questions": ["Why probe from inside the cluster?"]},
    }
    manifests = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "web"}},
                "template": {
                    "metadata": {"labels": {"app": "web"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "web",
                                "image": "nginx:1.27-alpine",
                                "ports": [{"containerPort": 80}],
                            }
                        ]
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "web", "namespace": namespace},
            "spec": {"selector": {"app": "web"}, "ports": [{"port": 80, "targetPort": 80}]},
        },
    ]
    (lab_dir / "lab.yaml").write_text(yaml.safe_dump(lab, sort_keys=False), encoding="utf-8")
    (manifest_dir / "resources.yaml").write_text(
        yaml.safe_dump_all(manifests, sort_keys=False), encoding="utf-8"
    )
    return LabRegistry(root)


def _wait_for_nginx(gateway: KubernetesGateway, scope: SessionScope) -> None:
    deadline = time.monotonic() + 120
    last_reason: str | None = None
    while time.monotonic() < deadline:
        pods = gateway.validation_pods(scope, {"app": "web"})
        if pods and all(pod.ready for pod in pods):
            return
        reasons = [container.reason for pod in pods for container in pod.containers]
        last_reason = next((reason for reason in reasons if reason), last_reason)
        if last_reason in {"ErrImagePull", "ImagePullBackOff"}:
            pytest.skip(f"nginx image could not be pulled: {last_reason}")
        time.sleep(2)
    pytest.skip(f"nginx did not become Ready; integration environment reason: {last_reason}")
