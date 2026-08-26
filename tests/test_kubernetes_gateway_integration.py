"""Opt-in WSL integration test for the trusted local minikube boundary."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from kubelab.config import load_config, resolve_kubeconfig_path
from kubelab.context_trust import (
    build_context_trust_service,
    trusted_context_fingerprint,
)
from kubelab.kubernetes_gateway import KubernetesGateway, SessionScope

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
