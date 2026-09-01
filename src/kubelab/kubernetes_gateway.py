"""Safe, namespaced Kubernetes access for KubeLab application services."""

from __future__ import annotations

import base64
import binascii
import copy
import hmac
import ipaddress
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast
from urllib.parse import urlsplit
from uuid import uuid4

from kubernetes import client, config, dynamic
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from urllib3.exceptions import ConnectTimeoutError, ReadTimeoutError

from kubelab.lab_registry import ExecutableLab
from kubelab.lab_schema import HttpTarget
from kubelab.manifest_security import ALLOWED_RESOURCES, ManifestDocument

FIELD_MANAGER = "kubelab"
MANAGED_BY_LABEL = "kubelab.io/managed-by"
LAB_ID_LABEL = "kubelab.io/lab-id"
SESSION_ID_ANNOTATION = "kubelab.io/session-id"
CONTEXT_FINGERPRINT_ANNOTATION = "kubelab.io/context-fingerprint"
PROBE_LABEL = "kubelab.io/probe"
WORKSPACE_NAME = "kubelab-workspace"
WORKSPACE_TOKEN_AUDIENCE = "https://kubernetes.default.svc.cluster.local"
WORKSPACE_TOKEN_SECONDS = 3600

_MAX_LOG_LINES = 500
_DEFAULT_LOG_LINES = 200
_MAX_LOG_BYTES = 256 * 1024
_INGRESS_CONTROLLER_NAMESPACE = "ingress-nginx"
_INGRESS_CONTROLLER_SERVICE = "ingress-nginx-controller"
_INGRESS_CONTROLLER_HOST = "ingress-nginx-controller.ingress-nginx.svc.cluster.local"
_APPLY_ORDER = {
    "ConfigMap": 0,
    "Secret": 0,
    "PersistentVolumeClaim": 0,
    "Service": 1,
    "Pod": 2,
    "Deployment": 2,
    "StatefulSet": 2,
    "DaemonSet": 2,
    "Job": 2,
    "CronJob": 2,
    "Ingress": 3,
}


class GatewayErrorCode(StrEnum):
    """Stable application-facing Kubernetes failure categories."""

    TIMEOUT = "KUBERNETES_TIMEOUT"
    UNAUTHORIZED = "KUBERNETES_UNAUTHORIZED"
    FORBIDDEN = "KUBERNETES_FORBIDDEN"
    NOT_FOUND = "KUBERNETES_NOT_FOUND"
    CONFLICT = "KUBERNETES_CONFLICT"
    API_ERROR = "KUBERNETES_API_ERROR"
    SCOPE_INVALID = "SESSION_SCOPE_INVALID"
    OWNERSHIP_MISMATCH = "NAMESPACE_OWNERSHIP_MISMATCH"
    NAMESPACE_TERMINATING = "NAMESPACE_TERMINATING"
    LOG_CONTAINER_REQUIRED = "LOG_CONTAINER_REQUIRED"
    INGRESS_CONTROLLER_UNAVAILABLE = "INGRESS_CONTROLLER_UNAVAILABLE"


class KubernetesGatewayError(RuntimeError):
    """Sanitized error returned by the Kubernetes boundary."""

    def __init__(
        self,
        code: GatewayErrorCode,
        message: str,
        *,
        retryable: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.context = dict(context or {})


class GatewayModel(BaseModel):
    """Immutable, serialization-safe DTO base."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionScope(GatewayModel):
    """Database-derived authorization boundary required by every cluster write."""

    lab_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    session_id: str
    namespace: str = Field(pattern=r"^kubelab-[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")
    context_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("session_id")
    @classmethod
    def session_id_is_uuid4(cls, value: str) -> str:
        from uuid import UUID

        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value.lower():
            raise ValueError("session_id must be a canonical UUID4 string")
        return value.lower()


class ResourceCondition(GatewayModel):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class ResourceSummary(GatewayModel):
    api_version: str
    kind: str
    namespace: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    status: str | None = None
    conditions: tuple[ResourceCondition, ...] = ()
    created_at: str | None = None
    secret_type: str | None = None
    secret_keys: tuple[str, ...] = ()


class ContainerSummary(GatewayModel):
    name: str
    image: str
    ready: bool
    restart_count: int
    state: str | None = None
    reason: str | None = None


class PodSummary(GatewayModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    phase: str | None = None
    ready: bool
    restart_count: int
    node_name: str | None = None
    containers: tuple[ContainerSummary, ...]
    reason: str | None = None


class EventSummary(GatewayModel):
    type: str | None = None
    reason: str | None = None
    message: str | None = None
    involved_kind: str | None = None
    involved_name: str | None = None
    count: int | None = None
    occurred_at: str | None = None


class LogResult(GatewayModel):
    pod: str
    container: str | None
    previous: bool
    content: str
    truncated: bool
    line_count: int


class NamespaceDeleteResult(GatewayModel):
    namespace: str
    deleted: bool
    already_absent: bool = False


class WorkspaceAccess(GatewayModel):
    """Ephemeral credentials for one namespace-restricted troubleshooting shell."""

    session_id: str
    namespace: str
    service_account: str = WORKSPACE_NAME
    token: SecretStr = Field(exclude=True, repr=False)


class ProbeSpec(GatewayModel):
    name: str = Field(max_length=63, pattern=r"^kubelab-probe-[a-z0-9-]+$")
    image: Literal["curlimages/curl:8.12.1"] = "curlimages/curl:8.12.1"
    url: str = Field(min_length=1, max_length=2048)
    host_header: str | None = Field(default=None, pattern=r"^[^\r\n:]+(?::\d+)?$")
    timeout_seconds: int = Field(default=15, ge=1, le=60)

    @field_validator("url")
    @classmethod
    def url_is_cluster_internal(cls, value: str) -> str:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not (
            hostname.endswith(".svc") or hostname.endswith(".svc.cluster.local")
        ):
            raise ValueError("probe URL must target a cluster-internal Service DNS name")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("probe URL must not contain credentials or fragments")
        return value


class DnsProbeSpec(GatewayModel):
    name: str = Field(max_length=63, pattern=r"^kubelab-probe-[a-z0-9-]+$")
    image: Literal["busybox:1.36.1"] = "busybox:1.36.1"
    fqdn: str = Field(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.svc\.cluster\.local$",
    )
    timeout_seconds: int = Field(default=15, ge=1, le=60)


class ConfigMatchResult(GatewayModel):
    resource_exists: bool
    key_exists: bool
    matched: bool
    valid_encoding: bool = True


class HttpProbeResult(GatewayModel):
    target_available: bool = True
    status_code: int | None = None
    exit_code: int | None = None
    infrastructure_error: bool = False
    timed_out: bool = False
    reason: str | None = None
    cleanup_warning: str | None = None


class DnsProbeResult(GatewayModel):
    resolved: bool = False
    infrastructure_error: bool = False
    timed_out: bool = False
    cleanup_warning: str | None = None


class KubernetesApi(Protocol):
    """Narrow adapter used by the gateway and replaced by fakes in unit tests."""

    def create_namespace(self, body: Mapping[str, Any], *, timeout: int) -> None: ...

    def read_namespace(self, name: str, *, timeout: int) -> Mapping[str, Any]: ...

    def get_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        name: str,
        timeout: int,
    ) -> Mapping[str, Any] | None: ...

    def list_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        label_selector: str | None,
        timeout: int,
    ) -> Sequence[Mapping[str, Any]]: ...

    def create_resource_quota(
        self, namespace: str, body: Mapping[str, Any], *, timeout: int
    ) -> None: ...

    def create_limit_range(
        self, namespace: str, body: Mapping[str, Any], *, timeout: int
    ) -> None: ...

    def apply(
        self,
        document: Mapping[str, Any],
        *,
        namespace: str,
        dry_run: bool,
        timeout: int,
    ) -> None: ...

    def delete_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        name: str,
        timeout: int,
    ) -> None: ...

    def delete_namespace(self, name: str, *, timeout: int) -> None: ...

    def list_resources(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]: ...

    def list_persistent_volumes(self, *, timeout: int) -> Sequence[Mapping[str, Any]]: ...

    def list_pods(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]: ...

    def read_pod(self, namespace: str, name: str, *, timeout: int) -> Mapping[str, Any]: ...

    def list_events(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]: ...

    def read_logs(
        self,
        namespace: str,
        pod: str,
        *,
        container: str | None,
        previous: bool,
        tail_lines: int,
        timeout: int,
    ) -> str: ...

    def create_probe(self, namespace: str, body: Mapping[str, Any], *, timeout: int) -> None: ...

    def delete_probe(self, namespace: str, name: str, *, timeout: int) -> None: ...

    def provision_workspace_access(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        expiration_seconds: int,
        timeout: int,
    ) -> str: ...

    def delete_workspace_access(self, namespace: str, *, timeout: int) -> None: ...

    def close(self) -> None: ...


class MaterializedManifests(Protocol):
    """Internal registry result that must never be serialized to a user boundary."""

    @property
    def documents(self) -> tuple[ManifestDocument, ...]: ...


class ManifestMaterializer(Protocol):
    """TOCTOU-resistant LabRegistry boundary used immediately before apply."""

    def materialize_for_gateway(self, loaded: ExecutableLab) -> MaterializedManifests: ...


class OfficialKubernetesApi:  # pragma: no cover - exercised by opt-in WSL integration tests
    """Official-client adapter created only from an explicit kubeconfig and context."""

    _RESOURCE_TYPES = (
        ("v1", "Service"),
        ("v1", "ConfigMap"),
        ("v1", "Secret"),
        ("v1", "PersistentVolumeClaim"),
        ("apps/v1", "Deployment"),
        ("apps/v1", "StatefulSet"),
        ("apps/v1", "DaemonSet"),
        ("batch/v1", "Job"),
        ("batch/v1", "CronJob"),
        ("networking.k8s.io/v1", "Ingress"),
    )

    def __init__(self, kubeconfig_path: Path, context_name: str) -> None:
        self._api_client = config.new_client_from_config(
            config_file=str(kubeconfig_path), context=context_name
        )
        self._core = client.CoreV1Api(self._api_client)
        self._dynamic = dynamic.DynamicClient(self._api_client)

    def create_namespace(self, body: Mapping[str, Any], *, timeout: int) -> None:
        self._core.create_namespace(body=body, _request_timeout=timeout)

    def read_namespace(self, name: str, *, timeout: int) -> Mapping[str, Any]:
        value = self._core.read_namespace(name, _request_timeout=timeout)
        return self._serialized(value)

    def get_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        name: str,
        timeout: int,
    ) -> Mapping[str, Any] | None:
        resource = self._dynamic.resources.get(api_version=api_version, kind=kind)
        try:
            response = resource.get(name=name, namespace=namespace, _request_timeout=timeout)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return self._serialized(response)

    def list_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        label_selector: str | None,
        timeout: int,
    ) -> Sequence[Mapping[str, Any]]:
        resource = self._dynamic.resources.get(api_version=api_version, kind=kind)
        kwargs: dict[str, Any] = {
            "namespace": namespace,
            "_request_timeout": timeout,
        }
        if label_selector:
            kwargs["label_selector"] = label_selector
        response = resource.get(**kwargs)
        return [self._serialized(item) for item in response.items]

    def create_resource_quota(
        self, namespace: str, body: Mapping[str, Any], *, timeout: int
    ) -> None:
        self._core.create_namespaced_resource_quota(namespace, body=body, _request_timeout=timeout)

    def create_limit_range(self, namespace: str, body: Mapping[str, Any], *, timeout: int) -> None:
        self._core.create_namespaced_limit_range(namespace, body=body, _request_timeout=timeout)

    def apply(
        self,
        document: Mapping[str, Any],
        *,
        namespace: str,
        dry_run: bool,
        timeout: int,
    ) -> None:
        resource = self._dynamic.resources.get(
            api_version=str(document["apiVersion"]), kind=str(document["kind"])
        )
        metadata = cast(Mapping[str, Any], document["metadata"])
        kwargs: dict[str, Any] = {
            "name": str(metadata["name"]),
            "namespace": namespace,
            "body": document,
            "content_type": "application/apply-patch+yaml",
            "field_manager": FIELD_MANAGER,
            "force": False,
            "_request_timeout": timeout,
        }
        if dry_run:
            kwargs["dry_run"] = "All"
        resource.patch(**kwargs)

    def delete_resource(
        self,
        api_version: str,
        kind: str,
        *,
        namespace: str,
        name: str,
        timeout: int,
    ) -> None:
        resource = self._dynamic.resources.get(api_version=api_version, kind=kind)
        try:
            resource.delete(name=name, namespace=namespace, _request_timeout=timeout)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def delete_namespace(self, name: str, *, timeout: int) -> None:
        self._core.delete_namespace(name, _request_timeout=timeout)

    def list_resources(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        documents: list[Mapping[str, Any]] = []
        for api_version, kind in self._RESOURCE_TYPES:
            resource = self._dynamic.resources.get(api_version=api_version, kind=kind)
            response = resource.get(namespace=namespace, _request_timeout=timeout)
            documents.extend(self._serialized(item) for item in response.items)
        return documents

    def list_persistent_volumes(self, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        response = self._core.list_persistent_volume(_request_timeout=timeout)
        return [self._serialized(item) for item in response.items]

    def list_pods(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        response = self._core.list_namespaced_pod(namespace, _request_timeout=timeout)
        return [self._serialized(item) for item in response.items]

    def read_pod(self, namespace: str, name: str, *, timeout: int) -> Mapping[str, Any]:
        return self._serialized(
            self._core.read_namespaced_pod(name, namespace, _request_timeout=timeout)
        )

    def list_events(self, namespace: str, *, timeout: int) -> Sequence[Mapping[str, Any]]:
        response = self._core.list_namespaced_event(namespace, _request_timeout=timeout)
        return [self._serialized(item) for item in response.items]

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
        return str(
            self._core.read_namespaced_pod_log(
                pod,
                namespace,
                container=container,
                previous=previous,
                tail_lines=tail_lines,
                _request_timeout=timeout,
            )
        )

    def create_probe(self, namespace: str, body: Mapping[str, Any], *, timeout: int) -> None:
        self._core.create_namespaced_pod(namespace, body=body, _request_timeout=timeout)

    def delete_probe(self, namespace: str, name: str, *, timeout: int) -> None:
        self._core.delete_namespaced_pod(name, namespace, _request_timeout=timeout)

    def provision_workspace_access(
        self,
        namespace: str,
        *,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        expiration_seconds: int,
        timeout: int,
    ) -> str:
        for document in _workspace_manifests(namespace, labels, annotations):
            self._apply_platform_resource(document, namespace=namespace, timeout=timeout)
        request = client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(
                audiences=[WORKSPACE_TOKEN_AUDIENCE],
                expiration_seconds=expiration_seconds,
            )
        )
        response = self._core.create_namespaced_service_account_token(
            WORKSPACE_NAME,
            namespace,
            body=request,
            _request_timeout=timeout,
        )
        token = getattr(getattr(response, "status", None), "token", None)
        if not isinstance(token, str) or not token:
            raise KubernetesGatewayError(
                GatewayErrorCode.API_ERROR,
                "Kubernetes did not return a workspace token.",
            )
        return token

    def delete_workspace_access(self, namespace: str, *, timeout: int) -> None:
        for api_version, kind in (
            ("rbac.authorization.k8s.io/v1", "RoleBinding"),
            ("rbac.authorization.k8s.io/v1", "Role"),
            ("v1", "ServiceAccount"),
        ):
            resource = self._dynamic.resources.get(api_version=api_version, kind=kind)
            try:
                resource.delete(
                    name=WORKSPACE_NAME,
                    namespace=namespace,
                    _request_timeout=timeout,
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise

    def _apply_platform_resource(
        self, document: Mapping[str, Any], *, namespace: str, timeout: int
    ) -> None:
        resource = self._dynamic.resources.get(
            api_version=str(document["apiVersion"]), kind=str(document["kind"])
        )
        resource.patch(
            name=WORKSPACE_NAME,
            namespace=namespace,
            body=document,
            content_type="application/apply-patch+yaml",
            field_manager=f"{FIELD_MANAGER}-workspace",
            force=False,
            _request_timeout=timeout,
        )

    def close(self) -> None:
        self._api_client.close()

    def _serialized(self, value: Any) -> Mapping[str, Any]:
        try:
            result = self._api_client.sanitize_for_serialization(value)
        except AttributeError:
            to_dict = getattr(value, "to_dict", None)
            if not callable(to_dict):
                raise
            result = to_dict()
        if not isinstance(result, Mapping):
            raise TypeError("Kubernetes API response did not serialize to an object")
        return cast(Mapping[str, Any], result)


T = TypeVar("T")


@dataclass(frozen=True)
class _Ownership:
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    finalizers: tuple[str, ...]


class KubernetesGateway:
    """Enforce KubeLab namespace ownership around all Kubernetes access."""

    def __init__(
        self,
        api: KubernetesApi,
        *,
        context_fingerprint: str,
        request_timeout_seconds: int = 10,
        max_log_bytes: int = _MAX_LOG_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", context_fingerprint) is None:
            raise ValueError("context_fingerprint must be a SHA256 digest")
        if request_timeout_seconds < 1:
            raise ValueError("request_timeout_seconds must be positive")
        if max_log_bytes < 1 or max_log_bytes > _MAX_LOG_BYTES:
            raise ValueError("max_log_bytes must be between 1 and 262144")
        self._api = api
        self._context_fingerprint = context_fingerprint
        self._request_timeout = request_timeout_seconds
        self._max_log_bytes = max_log_bytes
        self._monotonic = monotonic
        self._sleep = sleep

    @classmethod
    def from_kubeconfig(
        cls,
        *,
        kubeconfig_path: Path,
        context_name: str,
        context_fingerprint: str,
    ) -> KubernetesGateway:
        """Build without any in-cluster configuration fallback."""
        return cls(
            OfficialKubernetesApi(kubeconfig_path, context_name),
            context_fingerprint=context_fingerprint,
        )

    def close(self) -> None:
        self._api.close()

    def create_environment(self, scope: SessionScope) -> None:
        """Create the owned Namespace, ResourceQuota, and LimitRange."""
        self._guard_scope(scope)
        self._call(
            lambda: self._api.create_namespace(
                _namespace_manifest(scope), timeout=self._request_timeout
            )
        )
        self._call(
            lambda: self._api.create_resource_quota(
                scope.namespace, _resource_quota(scope), timeout=self._request_timeout
            )
        )
        self._call(
            lambda: self._api.create_limit_range(
                scope.namespace, _limit_range(scope), timeout=self._request_timeout
            )
        )

    def apply_lab(
        self, scope: SessionScope, loaded: ExecutableLab, registry: ManifestMaterializer
    ) -> None:
        """Materialize through LabRegistry, dry-run all objects, then apply in safe order."""
        self._guard_scope(scope)
        if loaded.definition.metadata.id != scope.lab_id:
            raise self._scope_error("The loaded lab does not match the Session scope.")
        if loaded.definition.environment.namespace != scope.namespace:
            raise self._scope_error("The lab Namespace does not match the Session scope.")
        materialized = registry.materialize_for_gateway(loaded)
        documents = tuple(materialized.documents)
        prepared = tuple(self._prepare_document(scope, document) for document in documents)
        for document in prepared:
            self._call(
                partial(
                    self._api.apply,
                    document,
                    namespace=scope.namespace,
                    dry_run=True,
                    timeout=self._request_timeout,
                )
            )
        for document in sorted(prepared, key=_apply_sort_key):
            self._call(
                partial(
                    self._api.apply,
                    document,
                    namespace=scope.namespace,
                    dry_run=False,
                    timeout=self._request_timeout,
                )
            )

    def apply_authoring_repair(
        self,
        scope: SessionScope,
        documents: tuple[ManifestDocument, ...],
        *,
        recreate: frozenset[tuple[str, str, str]] = frozenset(),
    ) -> None:
        """Apply only pre-scanned author repair documents inside an owned Namespace."""
        self.assert_namespace_owned(scope)
        prepared: list[Mapping[str, Any]] = []
        identities: set[tuple[str, str, str]] = set()
        for source in documents:
            api_version = source.data.get("apiVersion")
            kind = source.data.get("kind")
            metadata = source.data.get("metadata")
            name = metadata.get("name") if isinstance(metadata, Mapping) else None
            if (
                not isinstance(api_version, str)
                or not isinstance(kind, str)
                or not isinstance(name, str)
                or (api_version, kind) not in ALLOWED_RESOURCES
                or kind == "Secret"
            ):
                raise self._scope_error("Author repair contains an unsupported resource.")
            identity = (api_version, kind, name)
            if identity in identities:
                raise self._scope_error("Author repair contains a duplicate resource.")
            identities.add(identity)
            prepared.append(self._prepare_document(scope, source))
        if not recreate.issubset(identities):
            raise self._scope_error("Author recreate targets must exist in the repair bundle.")
        for api_version, kind, name in sorted(recreate):
            self._call(
                partial(
                    self._api.delete_resource,
                    api_version,
                    kind,
                    namespace=scope.namespace,
                    name=name,
                    timeout=self._request_timeout,
                )
            )
            deadline = self._monotonic() + 60
            while self._monotonic() < deadline:
                existing = self._call(
                    partial(
                        self._api.get_resource,
                        api_version,
                        kind,
                        namespace=scope.namespace,
                        name=name,
                        timeout=self._request_timeout,
                    )
                )
                if existing is None:
                    break
                self._sleep(min(1.0, max(0.0, deadline - self._monotonic())))
            else:
                raise KubernetesGatewayError(
                    GatewayErrorCode.TIMEOUT,
                    "Author repair resource deletion timed out.",
                    retryable=True,
                )
        for document in prepared:
            self._call(
                partial(
                    self._api.apply,
                    document,
                    namespace=scope.namespace,
                    dry_run=True,
                    timeout=self._request_timeout,
                )
            )
        for document in sorted(prepared, key=_apply_sort_key):
            self._call(
                partial(
                    self._api.apply,
                    document,
                    namespace=scope.namespace,
                    dry_run=False,
                    timeout=self._request_timeout,
                )
            )

    def namespace_exists(self, scope: SessionScope) -> bool:
        self._guard_scope(scope)
        try:
            self._call(
                lambda: self._api.read_namespace(scope.namespace, timeout=self._request_timeout)
            )
        except KubernetesGatewayError as exc:
            if exc.code is GatewayErrorCode.NOT_FOUND:
                return False
            raise
        return True

    def authoring_persistent_volume_residue(self, scope: SessionScope) -> tuple[str, ...]:
        """Return only PV names whose claimRef belongs to the author test Namespace."""
        self._guard_scope(scope)
        volumes = self._call(
            lambda: self._api.list_persistent_volumes(timeout=self._request_timeout)
        )
        names: list[str] = []
        for volume in volumes:
            spec = volume.get("spec")
            claim_ref = spec.get("claimRef") if isinstance(spec, Mapping) else None
            if not isinstance(claim_ref, Mapping) or claim_ref.get("namespace") != scope.namespace:
                continue
            metadata = volume.get("metadata")
            name = metadata.get("name") if isinstance(metadata, Mapping) else None
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(sorted(set(names)))

    def assert_namespace_owned(self, scope: SessionScope) -> None:
        """Apply all six deletion invariants without changing the cluster."""
        self._guard_scope(scope)
        if not scope.namespace.startswith("kubelab-"):
            raise self._ownership_error(scope, "Namespace prefix is not managed by KubeLab.")
        document = self._call(
            lambda: self._api.read_namespace(scope.namespace, timeout=self._request_timeout)
        )
        ownership = _ownership(document)
        expected = {
            MANAGED_BY_LABEL: "kubelab",
            LAB_ID_LABEL: scope.lab_id,
        }
        expected_annotations = {
            SESSION_ID_ANNOTATION: scope.session_id,
            CONTEXT_FINGERPRINT_ANNOTATION: scope.context_fingerprint,
        }
        if any(ownership.labels.get(key) != value for key, value in expected.items()) or any(
            ownership.annotations.get(key) != value for key, value in expected_annotations.items()
        ):
            raise self._ownership_error(
                scope, "Namespace ownership metadata does not match the Session record."
            )

    def provision_workspace(self, scope: SessionScope) -> WorkspaceAccess:
        """Create short-lived, namespace-only credentials after ownership verification."""
        self.assert_namespace_owned(scope)
        labels = {
            MANAGED_BY_LABEL: "kubelab",
            LAB_ID_LABEL: scope.lab_id,
        }
        annotations = {
            SESSION_ID_ANNOTATION: scope.session_id,
            CONTEXT_FINGERPRINT_ANNOTATION: scope.context_fingerprint,
        }
        token = self._call(
            lambda: self._api.provision_workspace_access(
                scope.namespace,
                labels=labels,
                annotations=annotations,
                expiration_seconds=WORKSPACE_TOKEN_SECONDS,
                timeout=self._request_timeout,
            )
        )
        return WorkspaceAccess(
            session_id=scope.session_id,
            namespace=scope.namespace,
            token=SecretStr(token),
        )

    def revoke_workspace(self, scope: SessionScope) -> None:
        """Remove only KubeLab's fixed workspace RBAC objects from an owned Namespace."""
        self.assert_namespace_owned(scope)
        self._call(
            lambda: self._api.delete_workspace_access(
                scope.namespace, timeout=self._request_timeout
            )
        )

    def delete_environment(
        self, scope: SessionScope, *, wait_timeout_seconds: float = 120
    ) -> NamespaceDeleteResult:
        """Delete only an exactly owned Namespace and never alter finalizers."""
        self._guard_scope(scope)
        try:
            self.assert_namespace_owned(scope)
        except KubernetesGatewayError as exc:
            if exc.code is GatewayErrorCode.NOT_FOUND:
                return NamespaceDeleteResult(
                    namespace=scope.namespace, deleted=False, already_absent=True
                )
            raise
        self._call(
            lambda: self._api.delete_namespace(scope.namespace, timeout=self._request_timeout)
        )
        deadline = self._monotonic() + wait_timeout_seconds
        while self._monotonic() < deadline:
            if not self.namespace_exists(scope):
                return NamespaceDeleteResult(namespace=scope.namespace, deleted=True)
            self._sleep(min(1.0, max(0.0, deadline - self._monotonic())))

        try:
            document = self._call(
                lambda: self._api.read_namespace(scope.namespace, timeout=self._request_timeout)
            )
        except KubernetesGatewayError as exc:
            if exc.code is GatewayErrorCode.NOT_FOUND:
                return NamespaceDeleteResult(namespace=scope.namespace, deleted=True)
            raise
        ownership = _ownership(document)
        remaining = self._safe_remaining_resource_names(scope)
        raise KubernetesGatewayError(
            GatewayErrorCode.NAMESPACE_TERMINATING,
            "Namespace deletion timed out; KubeLab will not remove finalizers automatically.",
            retryable=True,
            context={
                "namespace": scope.namespace,
                "finalizers": ownership.finalizers,
                "remaining_resources": remaining,
            },
        )

    def list_resources(self, scope: SessionScope) -> tuple[ResourceSummary, ...]:
        self.assert_namespace_owned(scope)
        documents = self._call(
            lambda: self._api.list_resources(scope.namespace, timeout=self._request_timeout)
        )
        summaries = [_resource_summary(item, scope.namespace) for item in documents]
        return tuple(sorted(summaries, key=lambda item: (item.kind, item.name)))

    def list_pods(self, scope: SessionScope) -> tuple[PodSummary, ...]:
        self.assert_namespace_owned(scope)
        documents = self._call(
            lambda: self._api.list_pods(scope.namespace, timeout=self._request_timeout)
        )
        return tuple(sorted((_pod_summary(item) for item in documents), key=lambda item: item.name))

    def resource_exists(
        self, scope: SessionScope, *, api_version: str, kind: str, name: str
    ) -> bool:
        """Check one allowlisted namespaced object without exposing its body."""
        self.assert_namespace_owned(scope)
        if (api_version, kind) not in ALLOWED_RESOURCES:
            raise self._scope_error("Validation requested an unsupported resource kind.")
        document = self._call(
            lambda: self._api.get_resource(
                api_version,
                kind,
                namespace=scope.namespace,
                name=name,
                timeout=self._request_timeout,
            )
        )
        return document is not None

    def validation_pods(
        self, scope: SessionScope, selector: Mapping[str, str]
    ) -> tuple[PodSummary, ...]:
        """Return safe Pod observations matching every requested label."""
        pods = self.list_pods(scope)
        return tuple(
            pod
            for pod in pods
            if all(pod.labels.get(key) == value for key, value in selector.items())
        )

    def deployment_available_replicas(self, scope: SessionScope, name: str) -> int | None:
        document = self._get_validation_resource(scope, "apps/v1", "Deployment", name)
        if document is None:
            return None
        value = _as_mapping(document.get("status")).get("availableReplicas", 0)
        return int(value) if isinstance(value, int) else 0

    def service_endpoint_count(self, scope: SessionScope, name: str) -> int | None:
        service = self._get_validation_resource(scope, "v1", "Service", name)
        if service is None:
            return None
        slices = self._call(
            lambda: self._api.list_resource(
                "discovery.k8s.io/v1",
                "EndpointSlice",
                namespace=scope.namespace,
                label_selector=f"kubernetes.io/service-name={name}",
                timeout=self._request_timeout,
            )
        )
        addresses: set[str] = set()
        for item in slices:
            for endpoint in _sequence_of_mappings(item.get("endpoints")):
                conditions = _as_mapping(endpoint.get("conditions"))
                if conditions.get("ready") is False:
                    continue
                for address in endpoint.get("addresses", ()):
                    if isinstance(address, str):
                        addresses.add(address)
        return len(addresses)

    def workload_container_image(
        self,
        scope: SessionScope,
        *,
        workload_kind: str,
        workload_name: str,
        container: str,
    ) -> str | None:
        api_versions = {
            "Pod": "v1",
            "Deployment": "apps/v1",
            "StatefulSet": "apps/v1",
            "DaemonSet": "apps/v1",
            "Job": "batch/v1",
            "CronJob": "batch/v1",
        }
        api_version = api_versions.get(workload_kind)
        if api_version is None:
            raise self._scope_error("Validation requested an unsupported workload kind.")
        document = self._get_validation_resource(scope, api_version, workload_kind, workload_name)
        if document is None:
            return None
        pod_spec = _workload_pod_spec(document, workload_kind)
        for item in _sequence_of_mappings(pod_spec.get("containers")):
            if item.get("name") == container:
                return _optional_string(item.get("image"))
        return None

    def config_value_matches(
        self,
        scope: SessionScope,
        *,
        source_kind: str,
        source_name: str,
        key: str,
        expected_value: str,
    ) -> ConfigMatchResult:
        if source_kind not in {"ConfigMap", "Secret"}:
            raise self._scope_error("Validation requested an unsupported configuration source.")
        document = self._get_validation_resource(scope, "v1", source_kind, source_name)
        if document is None:
            return ConfigMatchResult(resource_exists=False, key_exists=False, matched=False)
        data = _as_mapping(document.get("data"))
        raw = data.get(key)
        if not isinstance(raw, str):
            return ConfigMatchResult(resource_exists=True, key_exists=False, matched=False)
        if source_kind == "ConfigMap":
            return ConfigMatchResult(
                resource_exists=True,
                key_exists=True,
                matched=hmac.compare_digest(raw, expected_value),
            )
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError):
            return ConfigMatchResult(
                resource_exists=True,
                key_exists=True,
                matched=False,
                valid_encoding=False,
            )
        return ConfigMatchResult(
            resource_exists=True,
            key_exists=True,
            matched=hmac.compare_digest(decoded, expected_value),
        )

    def pvc_phase(self, scope: SessionScope, name: str) -> str | None:
        document = self._get_validation_resource(scope, "v1", "PersistentVolumeClaim", name)
        if document is None:
            return None
        return _optional_string(_as_mapping(document.get("status")).get("phase"))

    def run_http_probe(
        self, scope: SessionScope, target: HttpTarget, *, deadline: float
    ) -> HttpProbeResult:
        """Resolve a structured target, run curl in-cluster, and always clean the Pod."""
        self.assert_namespace_owned(scope)
        resolved = self._resolve_http_target(scope, target)
        if resolved is None:
            return HttpProbeResult(target_available=False, reason="Target route was not found.")
        url, host_header = resolved
        remaining = max(1, min(60, int(deadline - self._monotonic())))
        name = f"kubelab-probe-{uuid4().hex[:8]}"
        spec = ProbeSpec(
            name=name,
            url=url,
            host_header=host_header,
            timeout_seconds=remaining,
        )
        created = False
        result: HttpProbeResult | None = None
        cleanup_warning: str | None = None
        try:
            body = _probe_manifest(scope, spec)
            self._call(
                lambda: self._api.create_probe(scope.namespace, body, timeout=self._request_timeout)
            )
            created = True
            while self._monotonic() < deadline:
                document = self._call(
                    lambda: self._api.read_pod(scope.namespace, name, timeout=self._request_timeout)
                )
                phase = _optional_string(_as_mapping(document.get("status")).get("phase"))
                if phase in {"Succeeded", "Failed"}:
                    exit_code, reason = _probe_termination(document, container_name="curl")
                    pod_reason = _optional_string(_as_mapping(document.get("status")).get("reason"))
                    curl_completed = exit_code is not None and 0 <= exit_code <= 99
                    if (
                        exit_code is None
                        or pod_reason == "Evicted"
                        or (pod_reason == "DeadlineExceeded" and not curl_completed)
                        or reason
                        in {
                            "OOMKilled",
                            "ContainerCannotRun",
                            "StartError",
                        }
                    ):
                        result = HttpProbeResult(
                            exit_code=exit_code,
                            infrastructure_error=True,
                            timed_out=pod_reason == "DeadlineExceeded",
                            reason=pod_reason
                            or reason
                            or "Probe Pod failed before curl completed.",
                        )
                        break
                    raw = self._call(
                        lambda: self._api.read_logs(
                            scope.namespace,
                            name,
                            container="curl",
                            previous=False,
                            tail_lines=10,
                            timeout=self._request_timeout,
                        )
                    )
                    status_code = _parse_http_status(raw)
                    result = HttpProbeResult(
                        status_code=status_code,
                        exit_code=exit_code,
                        infrastructure_error=exit_code == 0 and status_code is None,
                        reason=reason,
                    )
                    break
                self._sleep(min(0.5, max(0.0, deadline - self._monotonic())))
            if result is None:
                result = HttpProbeResult(
                    infrastructure_error=True,
                    timed_out=True,
                    reason="Probe Pod did not complete before the deadline.",
                )
        finally:
            if created:
                try:
                    self._call(
                        lambda: self._api.delete_probe(
                            scope.namespace, name, timeout=self._request_timeout
                        )
                    )
                except KubernetesGatewayError as exc:
                    if exc.code is not GatewayErrorCode.NOT_FOUND:
                        cleanup_warning = "Probe cleanup failed; Namespace cleanup will retry."
        if result is None:  # pragma: no cover - defensive assignment
            raise KubernetesGatewayError(
                GatewayErrorCode.API_ERROR, "Probe execution produced no result."
            )
        return result.model_copy(update={"cleanup_warning": cleanup_warning})

    def run_dns_probe(
        self,
        scope: SessionScope,
        *,
        service: str,
        pod: str | None,
        deadline: float,
    ) -> DnsProbeResult:
        """Resolve only a server-constructed Kubernetes Service or stable Pod FQDN."""
        self.assert_namespace_owned(scope)
        prefix = f"{pod}." if pod is not None else ""
        fqdn = f"{prefix}{service}.{scope.namespace}.svc.cluster.local"
        pod_address: str | None = None
        headless_service = True
        if pod is not None:
            service_document = self._get_validation_resource(scope, "v1", "Service", service)
            headless_service = service_document is not None and (
                _as_mapping(service_document.get("spec")).get("clusterIP") == "None"
            )
            pod_document = self._get_validation_resource(scope, "v1", "Pod", pod)
            if pod_document is not None:
                pod_address = _optional_string(_as_mapping(pod_document.get("status")).get("podIP"))
        remaining = max(1, min(60, int(deadline - self._monotonic())))
        name = f"kubelab-probe-{uuid4().hex[:8]}"
        spec = DnsProbeSpec(name=name, fqdn=fqdn, timeout_seconds=remaining)
        created = False
        result: DnsProbeResult | None = None
        cleanup_warning: str | None = None
        try:
            body = _dns_probe_manifest(scope, spec)
            self._call(
                lambda: self._api.create_probe(scope.namespace, body, timeout=self._request_timeout)
            )
            created = True
            while self._monotonic() < deadline:
                document = self._call(
                    lambda: self._api.read_pod(scope.namespace, name, timeout=self._request_timeout)
                )
                phase = _optional_string(_as_mapping(document.get("status")).get("phase"))
                if phase in {"Succeeded", "Failed"}:
                    exit_code, reason = _probe_termination(document, container_name="dns")
                    pod_reason = _optional_string(_as_mapping(document.get("status")).get("reason"))
                    infrastructure_error = (
                        exit_code is None
                        or pod_reason == "Evicted"
                        or reason in {"OOMKilled", "ContainerCannotRun", "StartError"}
                    )
                    resolved = exit_code == 0
                    if resolved and pod is not None:
                        raw = self._call(
                            lambda: self._api.read_logs(
                                scope.namespace,
                                name,
                                container="dns",
                                previous=False,
                                tail_lines=20,
                                timeout=self._request_timeout,
                            )
                        )
                        resolved = (
                            headless_service
                            and pod_address is not None
                            and _dns_output_contains_address(raw, pod_address)
                        )
                    result = DnsProbeResult(
                        resolved=resolved,
                        infrastructure_error=infrastructure_error,
                        timed_out=pod_reason == "DeadlineExceeded",
                    )
                    break
                self._sleep(min(0.5, max(0.0, deadline - self._monotonic())))
            if result is None:
                result = DnsProbeResult(infrastructure_error=True, timed_out=True)
        finally:
            if created:
                try:
                    self._call(
                        lambda: self._api.delete_probe(
                            scope.namespace, name, timeout=self._request_timeout
                        )
                    )
                except KubernetesGatewayError as exc:
                    if exc.code is not GatewayErrorCode.NOT_FOUND:
                        cleanup_warning = "Probe cleanup failed; Namespace cleanup will retry."
        if result is None:  # pragma: no cover
            raise KubernetesGatewayError(
                GatewayErrorCode.API_ERROR, "Probe execution produced no result."
            )
        return result.model_copy(update={"cleanup_warning": cleanup_warning})

    def _get_validation_resource(
        self, scope: SessionScope, api_version: str, kind: str, name: str
    ) -> Mapping[str, Any] | None:
        self.assert_namespace_owned(scope)
        if (api_version, kind) not in ALLOWED_RESOURCES:
            raise self._scope_error("Validation requested an unsupported resource kind.")
        return self._call(
            lambda: self._api.get_resource(
                api_version,
                kind,
                namespace=scope.namespace,
                name=name,
                timeout=self._request_timeout,
            )
        )

    def _resolve_http_target(
        self, scope: SessionScope, target: HttpTarget
    ) -> tuple[str, str | None] | None:
        if target.mode == "service":
            service = self._get_validation_resource(scope, "v1", "Service", target.name)
            if service is None or not _service_has_port(service, target.port):
                return None
            return (
                f"{target.scheme}://{target.name}.{scope.namespace}.svc:{target.port}{target.path}",
                None,
            )

        ingress = self._get_validation_resource(
            scope, "networking.k8s.io/v1", "Ingress", target.name
        )
        if ingress is None:
            return None
        host = _ingress_host_for_target(ingress, target)
        if host is None:
            return None
        controller = self._call(
            lambda: self._api.get_resource(
                "v1",
                "Service",
                namespace=_INGRESS_CONTROLLER_NAMESPACE,
                name=_INGRESS_CONTROLLER_SERVICE,
                timeout=self._request_timeout,
            )
        )
        if controller is None:
            raise KubernetesGatewayError(
                GatewayErrorCode.INGRESS_CONTROLLER_UNAVAILABLE,
                "The minikube ingress-nginx controller Service is unavailable.",
                retryable=True,
            )
        controller_port = 443 if target.scheme == "https" else 80
        return (
            f"{target.scheme}://{_INGRESS_CONTROLLER_HOST}:{controller_port}{target.path}",
            host,
        )

    def list_events(self, scope: SessionScope) -> tuple[EventSummary, ...]:
        self.assert_namespace_owned(scope)
        documents = self._call(
            lambda: self._api.list_events(scope.namespace, timeout=self._request_timeout)
        )
        events = [_event_summary(item) for item in documents]
        return tuple(sorted(events, key=lambda item: item.occurred_at or ""))

    def read_logs(
        self,
        scope: SessionScope,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = _DEFAULT_LOG_LINES,
    ) -> LogResult:
        self.assert_namespace_owned(scope)
        if tail_lines < 1 or tail_lines > _MAX_LOG_LINES:
            raise ValueError("tail_lines must be between 1 and 500")
        pod_document = self._call(
            lambda: self._api.read_pod(scope.namespace, pod, timeout=self._request_timeout)
        )
        names = _container_names(pod_document)
        if container is None and len(names) > 1:
            raise KubernetesGatewayError(
                GatewayErrorCode.LOG_CONTAINER_REQUIRED,
                "A container must be selected for a multi-container Pod.",
                context={"pod": pod, "containers": names},
            )
        if container is not None and container not in names:
            raise KubernetesGatewayError(
                GatewayErrorCode.NOT_FOUND,
                "The selected container does not exist in the Pod.",
                context={"pod": pod, "container": container},
            )
        selected = container or (names[0] if names else None)
        raw = self._call(
            lambda: self._api.read_logs(
                scope.namespace,
                pod,
                container=selected,
                previous=previous,
                tail_lines=tail_lines,
                timeout=self._request_timeout,
            )
        )
        content, truncated = _truncate_log(
            raw, tail_lines=tail_lines, max_bytes=self._max_log_bytes
        )
        return LogResult(
            pod=pod,
            container=selected,
            previous=previous,
            content=content,
            truncated=truncated,
            line_count=len(content.splitlines()),
        )

    def create_probe(self, scope: SessionScope, spec: ProbeSpec) -> None:
        self.assert_namespace_owned(scope)
        hostname = urlsplit(spec.url).hostname or ""
        allowed_suffixes = (
            f".{scope.namespace}.svc",
            f".{scope.namespace}.svc.cluster.local",
        )
        if hostname != _INGRESS_CONTROLLER_HOST and not hostname.endswith(allowed_suffixes):
            raise self._scope_error("Probe target is outside the experiment Namespace.")
        body = _probe_manifest(scope, spec)
        self._call(
            lambda: self._api.create_probe(scope.namespace, body, timeout=self._request_timeout)
        )

    def delete_probe(self, scope: SessionScope, name: str) -> None:
        self.assert_namespace_owned(scope)
        if not name.startswith("kubelab-probe-"):
            raise self._scope_error("Only KubeLab probe Pods may be deleted.")
        try:
            self._call(
                lambda: self._api.delete_probe(scope.namespace, name, timeout=self._request_timeout)
            )
        except KubernetesGatewayError as exc:
            if exc.code is not GatewayErrorCode.NOT_FOUND:
                raise

    def _prepare_document(self, scope: SessionScope, source: ManifestDocument) -> Mapping[str, Any]:
        document = copy.deepcopy(dict(source.data))
        metadata_value = document.get("metadata")
        if not isinstance(metadata_value, Mapping):
            raise self._scope_error("Manifest metadata is missing.")
        metadata = dict(metadata_value)
        namespace = metadata.get("namespace")
        if namespace not in (None, scope.namespace):
            raise self._scope_error("Manifest targets a Namespace outside the Session scope.")
        metadata["namespace"] = scope.namespace
        document["metadata"] = metadata
        return document

    def _safe_remaining_resource_names(self, scope: SessionScope) -> tuple[str, ...]:
        try:
            documents = self._call(
                lambda: self._api.list_resources(scope.namespace, timeout=self._request_timeout)
            )
        except KubernetesGatewayError:
            return ()
        values = []
        for document in documents:
            metadata = _as_mapping(document.get("metadata"))
            values.append(f"{document.get('kind', 'Unknown')}/{metadata.get('name', 'unknown')}")
        return tuple(sorted(values))

    def _guard_scope(self, scope: SessionScope) -> None:
        if scope.context_fingerprint != self._context_fingerprint:
            raise self._scope_error("Session Context fingerprint does not match this client.")

    @staticmethod
    def _scope_error(message: str) -> KubernetesGatewayError:
        return KubernetesGatewayError(GatewayErrorCode.SCOPE_INVALID, message)

    @staticmethod
    def _ownership_error(scope: SessionScope, message: str) -> KubernetesGatewayError:
        return KubernetesGatewayError(
            GatewayErrorCode.OWNERSHIP_MISMATCH,
            message,
            context={"namespace": scope.namespace, "session_id": scope.session_id},
        )

    @staticmethod
    def _call(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except KubernetesGatewayError:
            raise
        except ApiException as exc:
            raise _translate_api_exception(exc) from exc
        except (TimeoutError, ConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
            raise KubernetesGatewayError(
                GatewayErrorCode.TIMEOUT,
                "Kubernetes API request timed out.",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise KubernetesGatewayError(
                GatewayErrorCode.API_ERROR,
                "Kubernetes API request failed.",
                retryable=True,
            ) from exc


def _translate_api_exception(error: ApiException) -> KubernetesGatewayError:
    mapping = {
        401: (GatewayErrorCode.UNAUTHORIZED, "Kubernetes credentials were rejected.", False),
        403: (GatewayErrorCode.FORBIDDEN, "Kubernetes access was forbidden.", False),
        404: (GatewayErrorCode.NOT_FOUND, "The Kubernetes resource was not found.", False),
        409: (GatewayErrorCode.CONFLICT, "The Kubernetes resource is in conflict.", True),
        408: (GatewayErrorCode.TIMEOUT, "Kubernetes API request timed out.", True),
        504: (GatewayErrorCode.TIMEOUT, "Kubernetes API request timed out.", True),
    }
    status = error.status if isinstance(error.status, int) else 0
    code, message, retryable = mapping.get(
        status,
        (GatewayErrorCode.API_ERROR, "Kubernetes API request failed.", status >= 500),
    )
    return KubernetesGatewayError(code, message, retryable=retryable)


def _namespace_manifest(scope: SessionScope) -> Mapping[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": scope.namespace,
            "labels": {
                MANAGED_BY_LABEL: "kubelab",
                LAB_ID_LABEL: scope.lab_id,
            },
            "annotations": {
                SESSION_ID_ANNOTATION: scope.session_id,
                CONTEXT_FINGERPRINT_ANNOTATION: scope.context_fingerprint,
            },
        },
    }


def _resource_quota(scope: SessionScope) -> Mapping[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "kubelab-quota", "labels": {MANAGED_BY_LABEL: "kubelab"}},
        "spec": {
            "hard": {
                "pods": "20",
                "services": "10",
                "persistentvolumeclaims": "5",
                "requests.storage": "2Gi",
            }
        },
    }


def _workspace_manifests(
    namespace: str,
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
) -> tuple[Mapping[str, Any], ...]:
    metadata = {
        "name": WORKSPACE_NAME,
        "namespace": namespace,
        "labels": dict(labels),
        "annotations": dict(annotations),
    }
    return (
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": copy.deepcopy(metadata),
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": copy.deepcopy(metadata),
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": [
                        "configmaps",
                        "events",
                        "persistentvolumeclaims",
                        "pods",
                        "services",
                    ],
                    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods/log"],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["limitranges", "resourcequotas"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": ["apps"],
                    "resources": ["daemonsets", "deployments", "replicasets", "statefulsets"],
                    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments/scale"],
                    "verbs": ["get", "update", "patch"],
                },
                {
                    "apiGroups": ["batch"],
                    "resources": ["cronjobs", "jobs"],
                    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["ingresses"],
                    "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": ["discovery.k8s.io"],
                    "resources": ["endpointslices"],
                    "verbs": ["get", "list", "watch"],
                },
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": copy.deepcopy(metadata),
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": WORKSPACE_NAME,
                    "namespace": namespace,
                }
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": WORKSPACE_NAME,
            },
        },
    )


def _limit_range(scope: SessionScope) -> Mapping[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "kubelab-limits", "labels": {MANAGED_BY_LABEL: "kubelab"}},
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
                    "default": {"cpu": "500m", "memory": "512Mi"},
                    "max": {"cpu": "2", "memory": "2Gi"},
                }
            ]
        },
    }


def _probe_manifest(scope: SessionScope, spec: ProbeSpec) -> Mapping[str, Any]:
    # Leave enough time for curl to report a network timeout and for the
    # kubelet to persist the terminated container state. If both deadlines
    # are identical, an expected unreachable target can be misclassified as
    # a probe-infrastructure DeadlineExceeded error instead of curl exit 28.
    curl_timeout_seconds = max(1, min(10, spec.timeout_seconds - 5))
    pod_deadline_seconds = min(spec.timeout_seconds, curl_timeout_seconds + 5)
    arguments = [
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        str(curl_timeout_seconds),
    ]
    if spec.host_header is not None:
        arguments.extend(["-H", f"Host: {spec.host_header}"])
    arguments.append(spec.url)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.name,
            "namespace": scope.namespace,
            "labels": {
                MANAGED_BY_LABEL: "kubelab",
                PROBE_LABEL: "true",
                LAB_ID_LABEL: scope.lab_id,
            },
            "annotations": {SESSION_ID_ANNOTATION: scope.session_id},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": pod_deadline_seconds,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 101,
                "runAsGroup": 102,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "curl",
                    "image": spec.image,
                    "args": arguments,
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "128Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                }
            ],
        },
    }


def _dns_probe_manifest(scope: SessionScope, spec: DnsProbeSpec) -> Mapping[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.name,
            "namespace": scope.namespace,
            "labels": {
                MANAGED_BY_LABEL: "kubelab",
                PROBE_LABEL: "true",
                LAB_ID_LABEL: scope.lab_id,
            },
            "annotations": {SESSION_ID_ANNOTATION: scope.session_id},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": spec.timeout_seconds,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "dns",
                    "image": spec.image,
                    "args": ["nslookup", spec.fqdn],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "16Mi"},
                        "limits": {"cpu": "50m", "memory": "64Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                }
            ],
        },
    }


def _apply_sort_key(document: Mapping[str, Any]) -> tuple[int, str, str]:
    metadata = _as_mapping(document.get("metadata"))
    kind = str(document.get("kind", ""))
    return (_APPLY_ORDER.get(kind, 99), kind, str(metadata.get("name", "")))


def _ownership(document: Mapping[str, Any]) -> _Ownership:
    metadata = _as_mapping(document.get("metadata"))
    labels = _string_mapping(metadata.get("labels"))
    annotations = _string_mapping(metadata.get("annotations"))
    spec = _as_mapping(document.get("spec"))
    finalizers_value = spec.get("finalizers", ())
    finalizers = (
        tuple(str(value) for value in finalizers_value)
        if isinstance(finalizers_value, Sequence) and not isinstance(finalizers_value, str)
        else ()
    )
    return _Ownership(labels, annotations, finalizers)


def _resource_summary(document: Mapping[str, Any], namespace: str) -> ResourceSummary:
    metadata = _as_mapping(document.get("metadata"))
    status_document = _as_mapping(document.get("status"))
    kind = str(document.get("kind", "Unknown"))
    conditions_value = status_document.get("conditions", ())
    conditions = (
        tuple(
            ResourceCondition(
                type=str(item.get("type", "Unknown")),
                status=str(item.get("status", "Unknown")),
                reason=_optional_string(item.get("reason")),
                message=_optional_string(item.get("message")),
            )
            for item in conditions_value
            if isinstance(item, Mapping)
        )
        if isinstance(conditions_value, Sequence) and not isinstance(conditions_value, str)
        else ()
    )
    status = next(
        (
            _optional_string(status_document.get(key))
            for key in ("phase", "readyReplicas", "availableReplicas")
            if status_document.get(key) is not None
        ),
        None,
    )
    data = _as_mapping(document.get("data")) if kind == "Secret" else {}
    string_data = _as_mapping(document.get("stringData")) if kind == "Secret" else {}
    return ResourceSummary(
        api_version=str(document.get("apiVersion", "")),
        kind=kind,
        namespace=_optional_string(metadata.get("namespace")) or namespace,
        name=str(metadata.get("name", "unknown")),
        labels=_string_mapping(metadata.get("labels")),
        status=status,
        conditions=conditions,
        created_at=_optional_string(metadata.get("creationTimestamp")),
        secret_type=_optional_string(document.get("type")) if kind == "Secret" else None,
        secret_keys=tuple(sorted({str(key) for key in (*data.keys(), *string_data.keys())})),
    )


def _pod_summary(document: Mapping[str, Any]) -> PodSummary:
    metadata = _as_mapping(document.get("metadata"))
    spec = _as_mapping(document.get("spec"))
    status = _as_mapping(document.get("status"))
    specs = {
        str(item.get("name")): item
        for item in spec.get("containers", ())
        if isinstance(item, Mapping) and item.get("name") is not None
    }
    containers: list[ContainerSummary] = []
    for item in status.get("containerStatuses", ()):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "unknown"))
        state, reason = _container_state(_as_mapping(item.get("state")))
        containers.append(
            ContainerSummary(
                name=name,
                image=str(_as_mapping(specs.get(name)).get("image", item.get("image", ""))),
                ready=bool(item.get("ready", False)),
                restart_count=int(item.get("restartCount", 0)),
                state=state,
                reason=reason,
            )
        )
    ready = bool(containers) and all(item.ready for item in containers)
    restart_count = sum(item.restart_count for item in containers)
    return PodSummary(
        name=str(metadata.get("name", "unknown")),
        labels=_string_mapping(metadata.get("labels")),
        phase=_optional_string(status.get("phase")),
        ready=ready,
        restart_count=restart_count,
        node_name=_optional_string(spec.get("nodeName")),
        containers=tuple(containers),
        reason=_optional_string(status.get("reason"))
        or next((item.reason for item in containers if item.reason), None),
    )


def _container_state(state: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for name in ("waiting", "running", "terminated"):
        value = state.get(name)
        if isinstance(value, Mapping):
            return name, _optional_string(value.get("reason"))
    return None, None


def _event_summary(document: Mapping[str, Any]) -> EventSummary:
    involved = _as_mapping(document.get("involvedObject"))
    occurred = next(
        (
            _optional_string(document.get(key))
            for key in ("eventTime", "lastTimestamp")
            if document.get(key) is not None
        ),
        None,
    )
    if occurred is None:
        occurred = _optional_string(_as_mapping(document.get("metadata")).get("creationTimestamp"))
    return EventSummary(
        type=_optional_string(document.get("type")),
        reason=_optional_string(document.get("reason")),
        message=_optional_string(document.get("message")),
        involved_kind=_optional_string(involved.get("kind")),
        involved_name=_optional_string(involved.get("name")),
        count=int(document["count"]) if isinstance(document.get("count"), int) else None,
        occurred_at=occurred,
    )


def _container_names(document: Mapping[str, Any]) -> tuple[str, ...]:
    spec = _as_mapping(document.get("spec"))
    return tuple(
        str(item["name"])
        for item in spec.get("containers", ())
        if isinstance(item, Mapping) and item.get("name") is not None
    )


def _sequence_of_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _workload_pod_spec(document: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    spec = _as_mapping(document.get("spec"))
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        job_template = _as_mapping(spec.get("jobTemplate"))
        job_spec = _as_mapping(job_template.get("spec"))
        template = _as_mapping(job_spec.get("template"))
        return _as_mapping(template.get("spec"))
    template = _as_mapping(spec.get("template"))
    return _as_mapping(template.get("spec"))


def _service_has_port(document: Mapping[str, Any], port: int) -> bool:
    spec = _as_mapping(document.get("spec"))
    return any(item.get("port") == port for item in _sequence_of_mappings(spec.get("ports")))


def _ingress_host_for_target(document: Mapping[str, Any], target: HttpTarget) -> str | None:
    spec = _as_mapping(document.get("spec"))
    for rule in _sequence_of_mappings(spec.get("rules")):
        host = rule.get("host")
        http = _as_mapping(rule.get("http"))
        for path in _sequence_of_mappings(http.get("paths")):
            backend = _as_mapping(path.get("backend"))
            service = _as_mapping(backend.get("service"))
            port = _as_mapping(service.get("port"))
            if (
                path.get("path") == target.path
                and service.get("name") is not None
                and port.get("number") == target.port
            ):
                return str(host) if isinstance(host, str) and host else "localhost"
    return None


def _probe_termination(
    document: Mapping[str, Any], *, container_name: Literal["curl", "dns"]
) -> tuple[int | None, str | None]:
    status = _as_mapping(document.get("status"))
    for container in _sequence_of_mappings(status.get("containerStatuses")):
        if container.get("name") != container_name:
            continue
        terminated = _as_mapping(_as_mapping(container.get("state")).get("terminated"))
        exit_code = terminated.get("exitCode")
        return (
            int(exit_code) if isinstance(exit_code, int) else None,
            _optional_string(terminated.get("reason")),
        )
    return None, _optional_string(status.get("reason"))


def _parse_http_status(value: str) -> int | None:
    candidate = value.strip().splitlines()[-1] if value.strip() else ""
    return int(candidate) if re.fullmatch(r"[1-5][0-9]{2}", candidate) else None


def _dns_output_contains_address(value: str, expected: str) -> bool:
    """Compare bounded probe output internally without returning DNS data."""
    try:
        expected_address = ipaddress.ip_address(expected)
    except ValueError:
        return False
    bounded, _ = _truncate_log(value, tail_lines=20, max_bytes=4096)
    for token in re.findall(r"[0-9A-Fa-f:.#]+", bounded):
        candidate = token.strip("[](),;")
        if "#" in candidate:
            candidate = candidate.split("#", maxsplit=1)[0]
        if expected_address.version == 4 and candidate.count(":") == 1:
            candidate = candidate.rsplit(":", maxsplit=1)[0]
        try:
            if ipaddress.ip_address(candidate) == expected_address:
                return True
        except ValueError:
            continue
    return False


def _truncate_log(content: str, *, tail_lines: int, max_bytes: int) -> tuple[str, bool]:
    lines = content.splitlines()
    truncated = len(lines) > tail_lines
    text = "\n".join(lines[-tail_lines:])
    payload = text.encode("utf-8")
    if len(payload) > max_bytes:
        truncated = True
        payload = payload[-max_bytes:]
        text = payload.decode("utf-8", errors="ignore")
        newline = text.find("\n")
        if newline >= 0:
            text = text[newline + 1 :]
    return text, truncated


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_mapping(value: Any) -> dict[str, str]:
    mapping = _as_mapping(value)
    return {str(key): str(item) for key, item in mapping.items()}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool, datetime)):
        return str(value)
    return None


__all__ = [
    "ContainerSummary",
    "ConfigMatchResult",
    "DnsProbeResult",
    "DnsProbeSpec",
    "EventSummary",
    "GatewayErrorCode",
    "KubernetesGateway",
    "KubernetesGatewayError",
    "HttpProbeResult",
    "LogResult",
    "NamespaceDeleteResult",
    "PodSummary",
    "ProbeSpec",
    "ResourceCondition",
    "ResourceSummary",
    "SessionScope",
    "WorkspaceAccess",
]
