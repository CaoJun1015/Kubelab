"""Static safety checks for declarative KubeLab Kubernetes manifests."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from kubernetes.utils.quantity import parse_quantity

ManifestErrorCode = Literal[
    "MANIFEST_YAML_INVALID",
    "MANIFEST_KIND_UNSUPPORTED",
    "MANIFEST_CLUSTER_SCOPED",
    "MANIFEST_NAMESPACE_FORBIDDEN",
    "MANIFEST_UNSAFE",
]

ALLOWED_RESOURCES = frozenset(
    {
        ("v1", "Pod"),
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
    }
)

KNOWN_CLUSTER_SCOPED_RESOURCES = frozenset(
    {
        ("v1", "Namespace"),
        ("v1", "Node"),
        ("v1", "PersistentVolume"),
        ("admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration"),
        ("admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration"),
        ("apiextensions.k8s.io/v1", "CustomResourceDefinition"),
        ("apiregistration.k8s.io/v1", "APIService"),
        ("certificates.k8s.io/v1alpha1", "ClusterTrustBundle"),
        ("flowcontrol.apiserver.k8s.io/v1", "FlowSchema"),
        ("flowcontrol.apiserver.k8s.io/v1", "PriorityLevelConfiguration"),
        ("networking.k8s.io/v1", "ClusterCIDR"),
        ("networking.k8s.io/v1", "ServiceCIDR"),
        ("rbac.authorization.k8s.io/v1", "ClusterRole"),
        ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
        ("scheduling.k8s.io/v1", "PriorityClass"),
        ("storage.k8s.io/v1", "CSIDriver"),
        ("storage.k8s.io/v1", "CSINode"),
        ("storage.k8s.io/v1", "StorageClass"),
        ("storage.k8s.io/v1", "VolumeAttachment"),
    }
)

_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)
_MAX_CPU = Decimal(2)
_MAX_MEMORY_BYTES = Decimal(2 * 1024**3)
_MAX_STORAGE_BYTES = Decimal(2 * 1024**3)


@dataclass(frozen=True)
class ManifestDocument:
    """One parsed YAML document plus its safe relative source path."""

    manifest_path: str
    document_index: int
    data: Mapping[str, Any]


@dataclass(frozen=True)
class ManifestIssue:
    """A redacted static safety failure."""

    code: ManifestErrorCode
    message: str
    manifest_path: str
    field_path: str


class ManifestSecurityScanner:
    """Reject manifests outside KubeLab's namespaced, low-privilege boundary."""

    def scan(
        self,
        documents: tuple[ManifestDocument, ...],
        *,
        namespace: str,
    ) -> tuple[ManifestIssue, ...]:
        issues: list[ManifestIssue] = []
        declared_objects = self._declared_objects(documents)
        service_names = {
            name
            for api_version, kind, name in declared_objects
            if api_version == "v1" and kind == "Service"
        }
        pvc_requests: list[tuple[ManifestDocument, str, Decimal]] = []

        for document in documents:
            data = document.data
            prefix = f"documents[{document.document_index}]"
            api_version = data.get("apiVersion")
            kind = data.get("kind")
            if not isinstance(api_version, str) or not isinstance(kind, str):
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_YAML_INVALID",
                        "Manifest apiVersion and kind must be strings.",
                        prefix,
                    )
                )
                continue

            resource = (api_version, kind)
            if resource in KNOWN_CLUSTER_SCOPED_RESOURCES:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_CLUSTER_SCOPED",
                        "Cluster-scoped resources are forbidden in lab manifests.",
                        f"{prefix}.kind",
                    )
                )
                continue
            if resource not in ALLOWED_RESOURCES:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_KIND_UNSUPPORTED",
                        "The manifest resource kind is not in the v1alpha1 allowlist.",
                        f"{prefix}.kind",
                    )
                )
                continue

            metadata = data.get("metadata")
            if not isinstance(metadata, Mapping) or not isinstance(metadata.get("name"), str):
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_YAML_INVALID",
                        "Manifest metadata.name is required.",
                        f"{prefix}.metadata.name",
                    )
                )
                continue

            explicit_namespace = metadata.get("namespace")
            if explicit_namespace is not None and explicit_namespace != namespace:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_NAMESPACE_FORBIDDEN",
                        "Manifest namespace must be omitted or match the lab namespace.",
                        f"{prefix}.metadata.namespace",
                    )
                )

            issues.extend(self._scan_owner_references(document, metadata, declared_objects, prefix))
            issues.extend(self._scan_external_urls(document, service_names, namespace, prefix))

            if kind == "Service":
                issues.extend(self._scan_service(document, data, prefix))
            if kind == "PersistentVolumeClaim":
                request = self._pvc_storage_request(document, data, prefix, issues)
                if request is not None:
                    pvc_requests.append(
                        (document, f"{prefix}.spec.resources.requests.storage", request)
                    )

            pod_spec = self._pod_spec(data, kind)
            if pod_spec is not None:
                spec, spec_path = pod_spec
                issues.extend(self._scan_pod_spec(document, spec, f"{prefix}.{spec_path}"))

        total_storage = sum((request for _, _, request in pvc_requests), Decimal(0))
        if total_storage > _MAX_STORAGE_BYTES and pvc_requests:
            document, path, _ = pvc_requests[-1]
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Total PVC storage requests exceed the v1alpha1 2Gi limit.",
                    path,
                )
            )
        return tuple(issues)

    @staticmethod
    def _declared_objects(
        documents: tuple[ManifestDocument, ...],
    ) -> set[tuple[str, str, str]]:
        declared: set[tuple[str, str, str]] = set()
        for document in documents:
            api_version = document.data.get("apiVersion")
            kind = document.data.get("kind")
            metadata = document.data.get("metadata")
            if (
                isinstance(api_version, str)
                and isinstance(kind, str)
                and isinstance(metadata, Mapping)
                and isinstance(metadata.get("name"), str)
            ):
                declared.add((api_version, kind, metadata["name"]))
        return declared

    def _scan_owner_references(
        self,
        document: ManifestDocument,
        metadata: Mapping[str, Any],
        declared_objects: set[tuple[str, str, str]],
        prefix: str,
    ) -> list[ManifestIssue]:
        issues: list[ManifestIssue] = []
        references = metadata.get("ownerReferences", [])
        if references is None:
            return issues
        if not isinstance(references, list):
            return [
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "ownerReferences must be a list of resources in the same lab.",
                    f"{prefix}.metadata.ownerReferences",
                )
            ]
        for index, reference in enumerate(references):
            path = f"{prefix}.metadata.ownerReferences[{index}]"
            if not isinstance(reference, Mapping):
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_UNSAFE",
                        "ownerReference must identify a resource in the same lab.",
                        path,
                    )
                )
                continue
            identity = (
                reference.get("apiVersion"),
                reference.get("kind"),
                reference.get("name"),
            )
            if identity not in declared_objects:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_UNSAFE",
                        "ownerReference points outside the lab manifest bundle.",
                        path,
                    )
                )
        return issues

    def _scan_service(
        self, document: ManifestDocument, data: Mapping[str, Any], prefix: str
    ) -> list[ManifestIssue]:
        spec = data.get("spec")
        if not isinstance(spec, Mapping):
            return []
        issues: list[ManifestIssue] = []
        service_type = spec.get("type", "ClusterIP")
        if service_type in {"NodePort", "LoadBalancer", "ExternalName"}:
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Service type can expose or redirect traffic outside the lab.",
                    f"{prefix}.spec.type",
                )
            )
        external_ips = spec.get("externalIPs")
        if isinstance(external_ips, list) and external_ips:
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Service externalIPs are forbidden.",
                    f"{prefix}.spec.externalIPs",
                )
            )
        return issues

    def _pvc_storage_request(
        self,
        document: ManifestDocument,
        data: Mapping[str, Any],
        prefix: str,
        issues: list[ManifestIssue],
    ) -> Decimal | None:
        value = _nested(data, "spec", "resources", "requests", "storage")
        if value is None:
            return None
        path = f"{prefix}.spec.resources.requests.storage"
        quantity = _safe_quantity(value)
        if quantity is None:
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "PVC storage request is not a valid Kubernetes quantity.",
                    path,
                )
            )
        return quantity

    def _scan_pod_spec(
        self,
        document: ManifestDocument,
        spec: Mapping[str, Any],
        prefix: str,
    ) -> list[ManifestIssue]:
        issues: list[ManifestIssue] = []
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(field) is True:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_UNSAFE",
                        "Host namespace sharing is forbidden.",
                        f"{prefix}.{field}",
                    )
                )

        volumes = spec.get("volumes", [])
        if isinstance(volumes, list):
            for index, volume in enumerate(volumes):
                if isinstance(volume, Mapping) and "hostPath" in volume:
                    issues.append(
                        self._issue(
                            document,
                            "MANIFEST_UNSAFE",
                            "hostPath volumes are forbidden.",
                            f"{prefix}.volumes[{index}].hostPath",
                        )
                    )

        pod_security = spec.get("securityContext")
        if isinstance(pod_security, Mapping):
            issues.extend(
                self._scan_security_context(document, pod_security, f"{prefix}.securityContext")
            )

        for collection in ("containers", "initContainers", "ephemeralContainers"):
            containers = spec.get(collection, [])
            if not isinstance(containers, list):
                continue
            for index, container in enumerate(containers):
                if not isinstance(container, Mapping):
                    continue
                container_path = f"{prefix}.{collection}[{index}]"
                security = container.get("securityContext")
                if isinstance(security, Mapping):
                    issues.extend(
                        self._scan_security_context(
                            document, security, f"{container_path}.securityContext"
                        )
                    )
                ports = container.get("ports", [])
                if isinstance(ports, list):
                    for port_index, port in enumerate(ports):
                        if isinstance(port, Mapping) and port.get("hostPort") not in (None, 0):
                            issues.append(
                                self._issue(
                                    document,
                                    "MANIFEST_UNSAFE",
                                    "Container hostPort is forbidden.",
                                    f"{container_path}.ports[{port_index}].hostPort",
                                )
                            )
                issues.extend(self._scan_container_resources(document, container, container_path))
        return issues

    def _scan_security_context(
        self,
        document: ManifestDocument,
        security: Mapping[str, Any],
        prefix: str,
    ) -> list[ManifestIssue]:
        issues: list[ManifestIssue] = []
        forbidden_true = {
            "privileged": "Privileged containers are forbidden.",
            "allowPrivilegeEscalation": "Privilege escalation is forbidden.",
        }
        for field, message in forbidden_true.items():
            if security.get(field) is True:
                issues.append(
                    self._issue(document, "MANIFEST_UNSAFE", message, f"{prefix}.{field}")
                )
        if security.get("procMount") == "Unmasked":
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Unmasked procMount is forbidden.",
                    f"{prefix}.procMount",
                )
            )
        seccomp = security.get("seccompProfile")
        if isinstance(seccomp, Mapping) and seccomp.get("type") == "Unconfined":
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Unconfined seccomp is forbidden.",
                    f"{prefix}.seccompProfile.type",
                )
            )
        windows_options = security.get("windowsOptions")
        if isinstance(windows_options, Mapping) and windows_options.get("hostProcess") is True:
            issues.append(
                self._issue(
                    document,
                    "MANIFEST_UNSAFE",
                    "Windows HostProcess is forbidden.",
                    f"{prefix}.windowsOptions.hostProcess",
                )
            )
        capabilities = security.get("capabilities")
        if isinstance(capabilities, Mapping):
            additions = capabilities.get("add")
            if isinstance(additions, list) and additions:
                issues.append(
                    self._issue(
                        document,
                        "MANIFEST_UNSAFE",
                        "Adding Linux capabilities is forbidden.",
                        f"{prefix}.capabilities.add",
                    )
                )
        return issues

    def _scan_container_resources(
        self,
        document: ManifestDocument,
        container: Mapping[str, Any],
        prefix: str,
    ) -> list[ManifestIssue]:
        issues: list[ManifestIssue] = []
        resources = container.get("resources")
        if not isinstance(resources, Mapping):
            return issues
        for boundary in ("requests", "limits"):
            values = resources.get(boundary)
            if not isinstance(values, Mapping):
                continue
            for resource, maximum in (("cpu", _MAX_CPU), ("memory", _MAX_MEMORY_BYTES)):
                if resource not in values:
                    continue
                path = f"{prefix}.resources.{boundary}.{resource}"
                quantity = _safe_quantity(values[resource])
                if quantity is None:
                    issues.append(
                        self._issue(
                            document,
                            "MANIFEST_UNSAFE",
                            "Container resource quantity is invalid.",
                            path,
                        )
                    )
                elif quantity > maximum:
                    issues.append(
                        self._issue(
                            document,
                            "MANIFEST_UNSAFE",
                            "Container resource request exceeds the v1alpha1 limit.",
                            path,
                        )
                    )
        return issues

    def _scan_external_urls(
        self,
        document: ManifestDocument,
        service_names: set[str],
        namespace: str,
        prefix: str,
    ) -> list[ManifestIssue]:
        issues: list[ManifestIssue] = []
        for path, text in _walk_strings(document.data, prefix):
            for match in _URL_PATTERN.finditer(text):
                if not _is_internal_url(match.group(0), service_names, namespace):
                    issues.append(
                        self._issue(
                            document,
                            "MANIFEST_UNSAFE",
                            "External URLs are forbidden in lab manifests.",
                            path,
                        )
                    )
                    break

        if document.data.get("kind") == "Secret":
            encoded_data = document.data.get("data")
            if isinstance(encoded_data, Mapping):
                for key, value in encoded_data.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        continue
                    try:
                        decoded = base64.b64decode(value, validate=True).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if any(
                        not _is_internal_url(match.group(0), service_names, namespace)
                        for match in _URL_PATTERN.finditer(decoded)
                    ):
                        issues.append(
                            self._issue(
                                document,
                                "MANIFEST_UNSAFE",
                                "External URLs are forbidden in Secret data.",
                                f"{prefix}.data.{key}",
                            )
                        )
        return issues

    @staticmethod
    def _pod_spec(data: Mapping[str, Any], kind: str) -> tuple[Mapping[str, Any], str] | None:
        paths: dict[str, tuple[str, ...]] = {
            "Pod": ("spec",),
            "Deployment": ("spec", "template", "spec"),
            "StatefulSet": ("spec", "template", "spec"),
            "DaemonSet": ("spec", "template", "spec"),
            "Job": ("spec", "template", "spec"),
            "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
        }
        path = paths.get(kind)
        if path is None:
            return None
        value = _nested(data, *path)
        if not isinstance(value, Mapping):
            return None
        return value, ".".join(path)

    @staticmethod
    def _issue(
        document: ManifestDocument,
        code: ManifestErrorCode,
        message: str,
        field_path: str,
    ) -> ManifestIssue:
        return ManifestIssue(code, message, document.manifest_path, field_path)


def _nested(data: Mapping[str, Any], *path: str) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _safe_quantity(value: Any) -> Decimal | None:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        return None
    try:
        return Decimal(parse_quantity(str(value)))
    except (ValueError, TypeError, InvalidOperation):
        return None


def _walk_strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                yield from _walk_strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}]")


def _is_internal_url(url: str, service_names: set[str], namespace: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.lower() == "file":
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in {"localhost", "127.0.0.1", "::1", "kubernetes.default.svc"}:
        return True
    allowed_hosts: set[str] = set()
    for service in service_names:
        allowed_hosts.update(
            {
                service,
                f"{service}.{namespace}",
                f"{service}.{namespace}.svc",
                f"{service}.{namespace}.svc.cluster.local",
            }
        )
    return host in allowed_hosts


__all__ = ["ManifestDocument", "ManifestIssue", "ManifestSecurityScanner"]
