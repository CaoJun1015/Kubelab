"""Ownership, safety, DTO, and error tests for KubernetesGateway."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from kubelab.kubernetes_gateway import (
    CONTEXT_FINGERPRINT_ANNOTATION,
    LAB_ID_LABEL,
    MANAGED_BY_LABEL,
    PROBE_LABEL,
    SESSION_ID_ANNOTATION,
    GatewayErrorCode,
    KubernetesGateway,
    KubernetesGatewayError,
    ProbeSpec,
    SessionScope,
    WorkspaceAccess,
)
from kubelab.lab_registry import LabRegistry
from kubelab.lab_schema import HttpTarget

FINGERPRINT = "a" * 64
SESSION_ID = "123e4567-e89b-42d3-a456-426614174000"


def scope(
    *,
    lab_id: str = "complete-lab",
    namespace: str = "kubelab-complete-lab",
    fingerprint: str = FINGERPRINT,
) -> SessionScope:
    return SessionScope(
        lab_id=lab_id,
        session_id=SESSION_ID,
        namespace=namespace,
        context_fingerprint=fingerprint,
    )


def owned_namespace(value: SessionScope, *, finalizers: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": value.namespace,
            "labels": {MANAGED_BY_LABEL: "kubelab", LAB_ID_LABEL: value.lab_id},
            "annotations": {
                SESSION_ID_ANNOTATION: value.session_id,
                CONTEXT_FINGERPRINT_ANNOTATION: value.context_fingerprint,
            },
        },
        "spec": {"finalizers": list(finalizers)},
    }


class FakeApi:
    def __init__(self, value: SessionScope | None = None) -> None:
        active = value or scope()
        self.namespace: Mapping[str, Any] | None = owned_namespace(active)
        self.created_namespace: Mapping[str, Any] | None = None
        self.quotas: list[Mapping[str, Any]] = []
        self.limits: list[Mapping[str, Any]] = []
        self.applies: list[tuple[bool, Mapping[str, Any]]] = []
        self.resources: list[Mapping[str, Any]] = []
        self.pods: list[Mapping[str, Any]] = []
        self.events: list[Mapping[str, Any]] = []
        self.logs = ""
        self.probes: list[Mapping[str, Any]] = []
        self.deleted_probes: list[str] = []
        self.workspace_documents: tuple[Mapping[str, Any], ...] = ()
        self.workspace_expiration_seconds: int | None = None
        self.workspace_deleted = False
        self.deleted_namespace = False
        self.disappear_after_reads: int | None = None
        self.reads_after_delete = 0
        self.apply_error: Exception | None = None
        self.delete_probe_error: Exception | None = None
        self.read_error: Exception | None = None
        self.closed = False
        self.last_log_request: tuple[str | None, bool, int] | None = None
        self.generic: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        self.generic_lists: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
        self.probe_reads: list[Mapping[str, Any]] = []

    def create_namespace(self, body: Mapping[str, Any], *, timeout: int) -> None:
        self.created_namespace = body
        self.namespace = body

    def read_namespace(self, name: str, *, timeout: int) -> Mapping[str, Any]:
        if self.read_error is not None:
            raise self.read_error
        if self.deleted_namespace and self.disappear_after_reads is not None:
            self.reads_after_delete += 1
            if self.reads_after_delete > self.disappear_after_reads:
                self.namespace = None
        if self.namespace is None:
            raise ApiException(status=404, reason="not found")
        return self.namespace

    def get_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        name: str,
        timeout: int,
    ) -> Mapping[str, Any] | None:
        return self.generic.get((api_version, kind, namespace, name))

    def list_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        label_selector: str | None,
        timeout: int,
    ) -> Sequence[Mapping[str, Any]]:
        del label_selector, timeout
        return self.generic_lists.get((api_version, kind, namespace), [])

    def create_resource_quota(
        self, namespace: str, body: Mapping[str, Any], *, timeout: int
    ) -> None:
        self.quotas.append(body)

    def create_limit_range(self, namespace: str, body: Mapping[str, Any], *, timeout: int) -> None:
        self.limits.append(body)

    def apply(
        self,
        document: Mapping[str, Any],
        *,
        namespace: str,
        dry_run: bool,
        timeout: int,
    ) -> None:
        if self.apply_error is not None:
            raise self.apply_error
        self.applies.append((dry_run, document))

    def delete_namespace(self, name: str, *, timeout: int) -> None:
        self.deleted_namespace = True

    def list_resources(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        return self.resources

    def list_pods(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        return self.pods

    def read_pod(self, namespace: str, name: str, *, timeout: int) -> Mapping[str, Any]:
        if name.startswith("kubelab-probe-") and self.probe_reads:
            return self.probe_reads.pop(0) if len(self.probe_reads) > 1 else self.probe_reads[0]
        for pod in self.pods:
            if pod.get("metadata", {}).get("name") == name:  # type: ignore[union-attr]
                return pod
        raise ApiException(status=404, reason="pod missing")

    def list_events(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        return self.events

    def read_logs(
        self,
        namespace: str,
        pod: str,
        *,
        container: str | None,
        previous: bool,
        tail_lines: int,
        timeout: int,
    ) -> str:
        self.last_log_request = (container, previous, tail_lines)
        return self.logs

    def create_probe(self, namespace: str, body: Mapping[str, Any], *, timeout: int) -> None:
        self.probes.append(body)

    def delete_probe(self, namespace: str, name: str, *, timeout: int) -> None:
        if self.delete_probe_error is not None:
            raise self.delete_probe_error
        self.deleted_probes.append(name)

    def provision_workspace_access(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        expiration_seconds: int,
        timeout: int,
    ) -> str:
        from kubelab.kubernetes_gateway import _workspace_manifests

        del timeout
        self.workspace_documents = _workspace_manifests(namespace, labels, annotations)
        self.workspace_expiration_seconds = expiration_seconds
        return "short-lived-workspace-token"

    def delete_workspace_access(self, namespace: str, *, timeout: int) -> None:
        del namespace, timeout
        self.workspace_deleted = True

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_session_scope_rejects_non_uuid4_and_context_fingerprint() -> None:
    with pytest.raises(ValidationError):
        SessionScope(
            lab_id="lab",
            session_id="not-a-uuid",
            namespace="kubelab-lab",
            context_fingerprint=FINGERPRINT,
        )
    with pytest.raises(ValidationError):
        scope(fingerprint="short")
    with pytest.raises(ValueError):
        KubernetesGateway(FakeApi(), context_fingerprint="A" * 64)
    with pytest.raises(ValueError):
        KubernetesGateway(FakeApi(), context_fingerprint=FINGERPRINT, max_log_bytes=0)
    with pytest.raises(ValueError):
        KubernetesGateway(FakeApi(), context_fingerprint=FINGERPRINT, request_timeout_seconds=0)


def test_create_environment_builds_owned_namespace_and_protection_resources() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    gateway.create_environment(scope())

    assert api.created_namespace is not None
    metadata = api.created_namespace["metadata"]
    assert metadata["labels"] == {
        MANAGED_BY_LABEL: "kubelab",
        LAB_ID_LABEL: "complete-lab",
    }
    assert metadata["annotations"][SESSION_ID_ANNOTATION] == SESSION_ID
    assert api.quotas[0]["spec"]["hard"] == {
        "pods": "20",
        "services": "10",
        "persistentvolumeclaims": "5",
        "requests.storage": "2Gi",
    }
    assert api.limits[0]["spec"]["limits"][0]["max"] == {"cpu": "2", "memory": "2Gi"}


def test_workspace_access_is_namespaced_secret_free_and_revoked() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    access = gateway.provision_workspace(scope())

    assert isinstance(access, WorkspaceAccess)
    assert access.namespace == "kubelab-complete-lab"
    assert "short-lived-workspace-token" not in repr(access)
    assert "token" not in access.model_dump()
    assert api.workspace_expiration_seconds == 3600
    assert [document["kind"] for document in api.workspace_documents] == [
        "ServiceAccount",
        "Role",
        "RoleBinding",
    ]
    role = api.workspace_documents[1]
    resources = {resource for rule in role["rules"] for resource in rule["resources"]}
    scale_rule = next(rule for rule in role["rules"] if rule["resources"] == ["deployments/scale"])
    assert scale_rule["verbs"] == ["get", "update", "patch"]
    assert "secrets" not in resources
    assert "roles" not in resources
    assert "rolebindings" not in resources
    assert all(
        document["metadata"]["namespace"] == scope().namespace
        for document in api.workspace_documents
    )

    gateway.revoke_workspace(scope())

    assert api.workspace_deleted is True


def test_context_fingerprint_mismatch_rejects_write_before_api_call() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)
    other = scope(fingerprint="b" * 64)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.create_environment(other)

    assert error.value.code is GatewayErrorCode.SCOPE_INVALID
    assert api.created_namespace is None


def test_apply_lab_dry_runs_every_document_then_uses_dependency_order() -> None:
    root = Path(__file__).parent / "fixtures" / "labs" / "valid"
    registry = LabRegistry(root)
    loaded = registry.scan().labs[0]
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    gateway.apply_lab(scope(), loaded, registry)

    assert [(dry, item["kind"]) for dry, item in api.applies] == [
        (True, "Deployment"),
        (True, "Service"),
        (False, "Service"),
        (False, "Deployment"),
    ]
    assert all(item["metadata"]["namespace"] == scope().namespace for _, item in api.applies)


def test_apply_lab_rejects_scope_mismatch_without_materializing() -> None:
    root = Path(__file__).parent / "fixtures" / "labs" / "valid"
    registry = LabRegistry(root)
    loaded = registry.scan().labs[0]
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.apply_lab(
            scope(lab_id="other-lab", namespace="kubelab-other-lab"), loaded, registry
        )

    assert error.value.code is GatewayErrorCode.SCOPE_INVALID
    assert api.applies == []


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, GatewayErrorCode.UNAUTHORIZED, False),
        (403, GatewayErrorCode.FORBIDDEN, False),
        (404, GatewayErrorCode.NOT_FOUND, False),
        (409, GatewayErrorCode.CONFLICT, True),
        (408, GatewayErrorCode.TIMEOUT, True),
        (504, GatewayErrorCode.TIMEOUT, True),
        (500, GatewayErrorCode.API_ERROR, True),
        (422, GatewayErrorCode.API_ERROR, False),
        (None, GatewayErrorCode.API_ERROR, False),
    ],
)
def test_api_errors_are_sanitized(
    status: int | None, code: GatewayErrorCode, retryable: bool
) -> None:
    api = FakeApi()
    api.apply_error = ApiException(status=status, reason="TOKEN super-secret")
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)
    root = Path(__file__).parent / "fixtures" / "labs" / "valid"
    registry = LabRegistry(root)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.apply_lab(scope(), registry.scan().labs[0], registry)

    assert error.value.code is code
    assert error.value.retryable is retryable
    assert "super-secret" not in str(error.value)
    assert error.value.context == {}


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("slow"), ConnectionError("offline"), RuntimeError("TOKEN private")],
)
def test_non_api_errors_are_sanitized(failure: Exception) -> None:
    api = FakeApi()
    api.read_error = failure
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.namespace_exists(scope())

    assert "private" not in str(error.value)
    assert error.value.code in {GatewayErrorCode.TIMEOUT, GatewayErrorCode.API_ERROR}


def test_namespace_exists_handles_not_found() -> None:
    api = FakeApi()
    api.namespace = None

    assert (
        KubernetesGateway(api, context_fingerprint=FINGERPRINT).namespace_exists(scope()) is False
    )


@pytest.mark.parametrize(
    ("metadata_key", "metadata_value"),
    [
        (MANAGED_BY_LABEL, "someone-else"),
        (LAB_ID_LABEL, "different-lab"),
        (SESSION_ID_ANNOTATION, "different-session"),
        (CONTEXT_FINGERPRINT_ANNOTATION, "b" * 64),
    ],
)
def test_namespace_ownership_mismatch_prevents_delete(
    metadata_key: str, metadata_value: str
) -> None:
    value = scope()
    api = FakeApi(value)
    document = owned_namespace(value)
    metadata = document["metadata"]
    target = (
        metadata["labels"]
        if metadata_key in {MANAGED_BY_LABEL, LAB_ID_LABEL}
        else metadata["annotations"]
    )
    target[metadata_key] = metadata_value
    api.namespace = document
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.delete_environment(value)

    assert error.value.code is GatewayErrorCode.OWNERSHIP_MISMATCH
    assert api.deleted_namespace is False


def test_delete_environment_handles_absent_namespace_idempotently() -> None:
    api = FakeApi()
    api.namespace = None
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    result = gateway.delete_environment(scope())

    assert result.already_absent is True
    assert result.deleted is False


def test_delete_environment_waits_until_namespace_disappears() -> None:
    api = FakeApi()
    api.disappear_after_reads = 1
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.delete_environment(scope(), wait_timeout_seconds=5)

    assert result.deleted is True
    assert api.deleted_namespace is True


def test_delete_environment_handles_disappearance_after_wait_deadline() -> None:
    api = FakeApi()
    api.disappear_after_reads = 0
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    result = gateway.delete_environment(scope(), wait_timeout_seconds=0)

    assert result.deleted is True


def test_terminating_timeout_reports_only_finalizers_and_resource_names() -> None:
    api = FakeApi()
    api.namespace = owned_namespace(scope(), finalizers=("kubernetes", "example/finalizer"))
    api.resources = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credential"},
            "data": {"token": "TOP-SECRET"},
        }
    ]
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.delete_environment(scope(), wait_timeout_seconds=2)

    assert error.value.code is GatewayErrorCode.NAMESPACE_TERMINATING
    assert error.value.context["finalizers"] == ("kubernetes", "example/finalizer")
    assert error.value.context["remaining_resources"] == ("Secret/credential",)
    assert "TOP-SECRET" not in repr(error.value.context)


def test_resource_summaries_hide_secret_values_and_sort() -> None:
    api = FakeApi()
    api.resources = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "credential",
                "namespace": scope().namespace,
                "creationTimestamp": "2026-01-01T00:00:00Z",
            },
            "type": "Opaque",
            "data": {"token": "TOP-SECRET"},
            "stringData": {"password": "plaintext"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "web", "labels": {"app": "web"}},
            "status": {
                "availableReplicas": 1,
                "conditions": [{"type": "Available", "status": "True", "reason": "Ready"}],
            },
        },
    ]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    summaries = gateway.list_resources(scope())
    output = repr(summaries)

    assert [item.kind for item in summaries] == ["Deployment", "Secret"]
    secret = summaries[1]
    assert secret.secret_keys == ("password", "token")
    assert secret.secret_type == "Opaque"
    assert "TOP-SECRET" not in output
    assert "plaintext" not in output
    assert summaries[0].conditions[0].reason == "Ready"


def pod_document(name: str = "web", *, containers: int = 1) -> dict[str, Any]:
    specs = [
        {"name": f"container-{index}", "image": f"nginx:{index}"} for index in range(containers)
    ]
    statuses = [
        {
            "name": f"container-{index}",
            "ready": index == 0,
            "restartCount": index,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}} if index else {"running": {}},
        }
        for index in range(containers)
    ]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name},
        "spec": {"nodeName": "minikube", "containers": specs},
        "status": {"phase": "Running", "containerStatuses": statuses},
    }


def test_pod_summaries_aggregate_readiness_restarts_and_reason() -> None:
    api = FakeApi()
    api.pods = [pod_document("z-pod", containers=2), pod_document("a-pod")]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    pods = gateway.list_pods(scope())

    assert [pod.name for pod in pods] == ["a-pod", "z-pod"]
    assert pods[1].ready is False
    assert pods[1].restart_count == 1
    assert pods[1].reason == "CrashLoopBackOff"
    assert pods[1].containers[0].image == "nginx:0"


def test_events_use_timestamp_fallback_and_sort_oldest_first() -> None:
    api = FakeApi()
    api.events = [
        {
            "metadata": {"creationTimestamp": "2026-01-03T00:00:00Z"},
            "reason": "Created",
            "involvedObject": {"kind": "Pod", "name": "web"},
        },
        {"eventTime": "2026-01-01T00:00:00Z", "reason": "Scheduled", "count": 1},
        {"lastTimestamp": "2026-01-02T00:00:00Z", "reason": "Pulled"},
    ]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    events = gateway.list_events(scope())

    assert [event.reason for event in events] == ["Scheduled", "Pulled", "Created"]
    assert events[2].involved_name == "web"


def test_logs_require_container_for_multi_container_pod() -> None:
    api = FakeApi()
    api.pods = [pod_document(containers=2)]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.read_logs(scope(), "web")

    assert error.value.code is GatewayErrorCode.LOG_CONTAINER_REQUIRED
    assert error.value.context["containers"] == ("container-0", "container-1")


def test_logs_validate_container_and_line_limit() -> None:
    api = FakeApi()
    api.pods = [pod_document()]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.read_logs(scope(), "web", container="missing")
    assert error.value.code is GatewayErrorCode.NOT_FOUND
    with pytest.raises(ValueError):
        gateway.read_logs(scope(), "web", tail_lines=501)


def test_logs_are_bounded_by_lines_and_bytes_without_persisting() -> None:
    api = FakeApi()
    api.pods = [pod_document()]
    api.logs = "\n".join(["old", "middle", "x" * 40, "new"])
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT, max_log_bytes=30)

    result = gateway.read_logs(scope(), "web", previous=True, tail_lines=3)

    assert result.truncated is True
    assert "old" not in result.content
    assert result.content.endswith("new")
    assert api.last_log_request == ("container-0", True, 3)


def test_probe_has_limits_hardening_labels_and_is_cleanable() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)
    spec = ProbeSpec(
        name="kubelab-probe-abc123",
        url="http://web.kubelab-complete-lab.svc:80/health",
    )

    gateway.create_probe(scope(), spec)
    gateway.delete_probe(scope(), spec.name)

    probe = api.probes[0]
    assert probe["metadata"]["labels"][PROBE_LABEL] == "true"
    assert probe["spec"]["automountServiceAccountToken"] is False
    assert probe["spec"]["containers"][0]["resources"]["limits"] == {
        "cpu": "100m",
        "memory": "128Mi",
    }
    assert probe["spec"]["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    arguments = probe["spec"]["containers"][0]["args"]
    assert arguments[arguments.index("--max-time") + 1] == "10"
    assert probe["spec"]["activeDeadlineSeconds"] == 15
    assert probe["spec"]["containers"][0]["args"][-1] == spec.url
    assert api.deleted_probes == [spec.name]


def test_probe_rejects_external_url_and_credentials() -> None:
    with pytest.raises(ValidationError):
        ProbeSpec(name="kubelab-probe-external", url="https://example.com/health")
    with pytest.raises(ValidationError):
        ProbeSpec(
            name="kubelab-probe-credential",
            url="http://user:password@web.namespace.svc/health",
        )


def test_probe_rejects_service_in_another_namespace() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)
    spec = ProbeSpec(
        name="kubelab-probe-cross-namespace",
        url="http://web.other-namespace.svc/health",
    )

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.create_probe(scope(), spec)

    assert error.value.code is GatewayErrorCode.SCOPE_INVALID
    assert api.probes == []


def test_probe_delete_rejects_non_platform_name_and_ignores_not_found() -> None:
    api = FakeApi()
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)
    with pytest.raises(KubernetesGatewayError) as error:
        gateway.delete_probe(scope(), "user-pod")
    assert error.value.code is GatewayErrorCode.SCOPE_INVALID

    api.delete_probe_error = ApiException(status=404)
    gateway.delete_probe(scope(), "kubelab-probe-missing")


def test_gateway_close_closes_client_adapter() -> None:
    api = FakeApi()

    KubernetesGateway(api, context_fingerprint=FINGERPRINT).close()

    assert api.closed is True


def test_validation_resource_queries_are_namespaced_and_safe() -> None:
    api = FakeApi()
    key = ("apps/v1", "Deployment", scope().namespace, "web")
    api.generic[key] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web"},
        "status": {"availableReplicas": 2},
    }
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    assert gateway.resource_exists(scope(), api_version="apps/v1", kind="Deployment", name="web")
    assert gateway.deployment_available_replicas(scope(), "web") == 2
    assert gateway.deployment_available_replicas(scope(), "missing") is None
    with pytest.raises(KubernetesGatewayError) as error:
        gateway.resource_exists(scope(), api_version="v1", kind="Namespace", name="default")
    assert error.value.code is GatewayErrorCode.SCOPE_INVALID


def test_validation_pods_filter_labels() -> None:
    api = FakeApi()
    matching = pod_document("matching")
    matching["metadata"]["labels"] = {"app": "web", "tier": "frontend"}
    other = pod_document("other")
    other["metadata"]["labels"] = {"app": "worker"}
    api.pods = [other, matching]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    pods = gateway.validation_pods(scope(), {"app": "web", "tier": "frontend"})

    assert [pod.name for pod in pods] == ["matching"]
    assert pods[0].labels == {"app": "web", "tier": "frontend"}


def test_endpoint_count_deduplicates_ready_addresses() -> None:
    api = FakeApi()
    api.generic[("v1", "Service", scope().namespace, "web")] = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web"},
    }
    api.generic_lists[("discovery.k8s.io/v1", "EndpointSlice", scope().namespace)] = [
        {
            "endpoints": [
                {"addresses": ["10.0.0.1"], "conditions": {"ready": True}},
                {"addresses": ["10.0.0.2"], "conditions": {"ready": False}},
            ]
        },
        {"endpoints": [{"addresses": ["10.0.0.1", "10.0.0.3"], "conditions": {}}]},
    ]
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    assert gateway.service_endpoint_count(scope(), "web") == 2
    assert gateway.service_endpoint_count(scope(), "missing") is None


@pytest.mark.parametrize(
    ("kind", "api_version", "spec"),
    [
        ("Pod", "v1", {"containers": [{"name": "web", "image": "nginx:1.27"}]}),
        (
            "Deployment",
            "apps/v1",
            {"template": {"spec": {"containers": [{"name": "web", "image": "nginx:1.27"}]}}},
        ),
        (
            "CronJob",
            "batch/v1",
            {
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {"containers": [{"name": "web", "image": "nginx:1.27"}]}
                        }
                    }
                }
            },
        ),
    ],
)
def test_workload_container_image_reads_pod_templates(
    kind: str, api_version: str, spec: dict[str, Any]
) -> None:
    api = FakeApi()
    api.generic[(api_version, kind, scope().namespace, "workload")] = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": "workload"},
        "spec": spec,
    }
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    assert (
        gateway.workload_container_image(
            scope(), workload_kind=kind, workload_name="workload", container="web"
        )
        == "nginx:1.27"
    )


def test_config_value_comparison_never_returns_values() -> None:
    api = FakeApi()
    api.generic[("v1", "ConfigMap", scope().namespace, "settings")] = {"data": {"mode": "practice"}}
    api.generic[("v1", "Secret", scope().namespace, "credential")] = {
        "data": {"token": "c3VwZXItc2VjcmV0"}
    }
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    config_result = gateway.config_value_matches(
        scope(),
        source_kind="ConfigMap",
        source_name="settings",
        key="mode",
        expected_value="practice",
    )
    secret_result = gateway.config_value_matches(
        scope(),
        source_kind="Secret",
        source_name="credential",
        key="token",
        expected_value="super-secret",
    )

    assert config_result.matched is True
    assert secret_result.matched is True
    assert "super-secret" not in repr(secret_result)


def test_secret_invalid_base64_and_pvc_phase_are_structured() -> None:
    api = FakeApi()
    api.generic[("v1", "Secret", scope().namespace, "broken")] = {"data": {"token": "%%%"}}
    api.generic[("v1", "PersistentVolumeClaim", scope().namespace, "data")] = {
        "status": {"phase": "Bound"}
    }
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    result = gateway.config_value_matches(
        scope(),
        source_kind="Secret",
        source_name="broken",
        key="token",
        expected_value="secret",
    )

    assert result.valid_encoding is False
    assert gateway.pvc_phase(scope(), "data") == "Bound"


def completed_probe(*, exit_code: int = 0, phase: str = "Succeeded") -> dict[str, Any]:
    return {
        "metadata": {"name": "probe"},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "curl",
                    "state": {"terminated": {"exitCode": exit_code, "reason": "Completed"}},
                }
            ],
        },
    }


def test_service_http_probe_returns_status_and_cleans_pod() -> None:
    api = FakeApi()
    api.generic[("v1", "Service", scope().namespace, "web")] = {"spec": {"ports": [{"port": 80}]}}
    api.probe_reads = [completed_probe()]
    api.logs = "200\n"
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.run_http_probe(
        scope(),
        HttpTarget(mode="service", name="web", port=80, path="/health"),
        deadline=5,
    )

    assert result.status_code == 200
    assert result.exit_code == 0
    assert api.deleted_probes[0].startswith("kubelab-probe-")
    assert api.probes[0]["spec"]["containers"][0]["args"][-1] == (
        "http://web.kubelab-complete-lab.svc:80/health"
    )
    assert api.probes[0]["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 101,
        "runAsGroup": 102,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def test_probe_timeout_and_cleanup_failure_are_reported_safely() -> None:
    api = FakeApi()
    api.generic[("v1", "Service", scope().namespace, "web")] = {"spec": {"ports": [{"port": 80}]}}
    api.probe_reads = [{"status": {"phase": "Pending"}}]
    api.delete_probe_error = ApiException(status=500, reason="TOKEN private")
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.run_http_probe(
        scope(), HttpTarget(mode="service", name="web", port=80), deadline=1
    )

    assert result.timed_out is True
    assert result.infrastructure_error is True
    assert result.cleanup_warning is not None
    assert "private" not in result.cleanup_warning


@pytest.mark.parametrize(
    ("document", "logs", "timed_out"),
    [
        ({"status": {"phase": "Failed", "reason": "DeadlineExceeded"}}, "", True),
        ({"status": {"phase": "Failed", "reason": "Evicted"}}, "", False),
        (completed_probe(exit_code=0), "not-an-http-status", False),
    ],
)
def test_probe_platform_failures_are_errors(
    document: dict[str, Any], logs: str, timed_out: bool
) -> None:
    api = FakeApi()
    api.generic[("v1", "Service", scope().namespace, "web")] = {"spec": {"ports": [{"port": 80}]}}
    api.probe_reads = [document]
    api.logs = logs
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.run_http_probe(
        scope(), HttpTarget(mode="service", name="web", port=80), deadline=5
    )

    assert result.infrastructure_error is True
    assert result.timed_out is timed_out
    assert api.deleted_probes


def test_probe_preserves_curl_network_failure_when_pod_deadline_is_also_reported() -> None:
    api = FakeApi()
    api.generic[("v1", "Service", scope().namespace, "web")] = {"spec": {"ports": [{"port": 80}]}}
    document = completed_probe(phase="Failed", exit_code=7)
    document["status"]["reason"] = "DeadlineExceeded"
    api.probe_reads = [document]
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.run_http_probe(
        scope(), HttpTarget(mode="service", name="web", port=80), deadline=5
    )

    assert result.exit_code == 7
    assert result.infrastructure_error is False
    assert result.timed_out is False
    assert api.deleted_probes


def test_ingress_probe_uses_only_builtin_controller_service() -> None:
    api = FakeApi()
    api.generic[("networking.k8s.io/v1", "Ingress", scope().namespace, "web")] = {
        "spec": {
            "rules": [
                {
                    "host": "app.test",
                    "http": {
                        "paths": [
                            {
                                "path": "/health",
                                "backend": {"service": {"name": "web", "port": {"number": 8080}}},
                            }
                        ]
                    },
                }
            ]
        }
    }
    api.generic[("v1", "Service", "ingress-nginx", "ingress-nginx-controller")] = {
        "metadata": {"name": "ingress-nginx-controller"}
    }
    api.probe_reads = [completed_probe()]
    api.logs = "200"
    clock = FakeClock()
    gateway = KubernetesGateway(
        api,
        context_fingerprint=FINGERPRINT,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = gateway.run_http_probe(
        scope(),
        HttpTarget(mode="ingress", name="web", port=8080, path="/health"),
        deadline=10,
    )

    args = api.probes[0]["spec"]["containers"][0]["args"]
    assert result.status_code == 200
    assert "Host: app.test" in args
    assert args[-1] == ("http://ingress-nginx-controller.ingress-nginx.svc.cluster.local:80/health")


def test_ingress_controller_missing_returns_retryable_error() -> None:
    api = FakeApi()
    api.generic[("networking.k8s.io/v1", "Ingress", scope().namespace, "web")] = {
        "spec": {
            "rules": [
                {
                    "host": "app.test",
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "backend": {"service": {"name": "web", "port": {"number": 80}}},
                            }
                        ]
                    },
                }
            ]
        }
    }
    gateway = KubernetesGateway(api, context_fingerprint=FINGERPRINT)

    with pytest.raises(KubernetesGatewayError) as error:
        gateway.run_http_probe(
            scope(), HttpTarget(mode="ingress", name="web", port=80), deadline=10
        )

    assert error.value.code is GatewayErrorCode.INGRESS_CONTROLLER_UNAVAILABLE
    assert error.value.retryable is True
