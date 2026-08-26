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
)
from kubelab.lab_registry import LabRegistry

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
        self.deleted_namespace = False
        self.disappear_after_reads: int | None = None
        self.reads_after_delete = 0
        self.apply_error: Exception | None = None
        self.delete_probe_error: Exception | None = None
        self.read_error: Exception | None = None
        self.closed = False
        self.last_log_request: tuple[str | None, bool, int] | None = None

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
