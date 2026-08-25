"""Read-only Kubernetes context identity and explicit local trust management."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

import yaml
from kubernetes import client
from kubernetes import config as kube_config
from pydantic import BaseModel, ConfigDict

from kubelab.config import (
    KubeLabConfig,
    ToolName,
    TrustedContext,
    get_config_path,
    load_config,
    resolve_kubeconfig_path,
    save_config,
)
from kubelab.tools import (
    CommandResult,
    LocatedTool,
    ProcessRunner,
    ToolExecutionError,
    ToolLocator,
)


class ContextError(RuntimeError):
    """Base error with a stable public code for context operations."""

    code = "CONTEXT_ERROR"


class ContextInspectionError(ContextError):
    """Raised when the current context identity cannot be read safely."""

    code = "CONTEXT_INSPECTION_FAILED"


class ContextNotLocalMinikubeError(ContextError):
    """Raised when a context cannot be proven to belong to local minikube."""

    code = "CONTEXT_NOT_LOCAL_MINIKUBE"


class ContextNotTrustedError(ContextError):
    """Raised before a write when the current context was never trusted."""

    code = "CONTEXT_NOT_TRUSTED"


class ContextFingerprintMismatchError(ContextError):
    """Raised before a write when the trusted cluster identity has drifted."""

    code = "CONTEXT_FINGERPRINT_MISMATCH"


class TrustState(StrEnum):
    """Relationship between the current identity and persisted trust."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    DRIFTED = "drifted"


class ContextIdentity(BaseModel):
    """Credential-free identity facts observed from kubeconfig and the API."""

    model_config = ConfigDict(extra="forbid")

    context_name: str
    api_server: str
    ca_sha256: str
    kube_system_uid: str
    kubernetes_version: str


class ContextInspection(BaseModel):
    """Stable public representation returned by context inspect."""

    model_config = ConfigDict(extra="forbid")

    context_name: str
    minikube_profile: str | None
    api_server: str
    ca_sha256: str
    kube_system_uid: str
    kubernetes_version: str
    trusted: bool
    trust_state: TrustState


class ClusterFacts(BaseModel):
    """Small API-derived portion of the context identity."""

    model_config = ConfigDict(extra="forbid")

    kube_system_uid: str
    kubernetes_version: str


class IdentityProvider(Protocol):
    """Source of the current credential-free Kubernetes identity."""

    def current_context_name(self) -> str: ...

    def inspect_current(self) -> ContextIdentity: ...


class ClusterProbe(Protocol):
    """Read-only Kubernetes API probe used after kubeconfig parsing."""

    def inspect(self, kubeconfig_path: Path, context_name: str) -> ClusterFacts: ...


class ProfileVerifier(Protocol):
    """Prove whether an identity belongs to an accessible local minikube profile."""

    def profile_for(self, identity: ContextIdentity) -> str | None: ...


class ToolFinder(Protocol):
    """Narrow tool discovery interface used by the minikube verifier."""

    def locate(self, name: ToolName) -> LocatedTool | None: ...


class CommandRunner(Protocol):
    """Shell-free command interface used by the minikube verifier."""

    def run(
        self,
        executable: Path,
        arguments: Sequence[str],
        *,
        timeout_seconds: int = 10,
    ) -> CommandResult: ...


class KubernetesClusterProbe:
    """Read server version and kube-system UID through the official client."""

    def inspect(
        self, kubeconfig_path: Path, context_name: str
    ) -> ClusterFacts:  # pragma: no cover - real cluster adapter
        api_client: client.ApiClient | None = None
        try:
            api_client = kube_config.new_client_from_config(
                config_file=str(kubeconfig_path), context=context_name
            )
            version = client.VersionApi(api_client).get_code(_request_timeout=10)
            namespace = client.CoreV1Api(api_client).read_namespace(
                "kube-system", _request_timeout=10
            )
            git_version = version.git_version
            uid = namespace.metadata.uid if namespace.metadata else None
            if not git_version or not uid:
                raise ContextInspectionError(
                    "Kubernetes API did not return a complete context identity."
                )
            return ClusterFacts(kube_system_uid=str(uid), kubernetes_version=git_version)
        except ContextInspectionError:
            raise
        except Exception as exc:
            raise ContextInspectionError("Kubernetes API identity inspection failed.") from exc
        finally:
            if api_client is not None:
                api_client.close()


class KubeconfigIdentityProvider:
    """Read current Context, Server, CA digest, and API identity without exposing credentials."""

    def __init__(self, kubeconfig_path: Path, cluster_probe: ClusterProbe) -> None:
        self._kubeconfig_path = kubeconfig_path
        self._cluster_probe = cluster_probe

    def current_context_name(self) -> str:
        """Return only the configured current context without contacting the cluster."""
        document = _load_kubeconfig(self._kubeconfig_path)
        return _required_string(document, "current-context", "current context")

    def inspect_current(self) -> ContextIdentity:
        """Build a credential-free identity for the active kubeconfig context."""
        document = _load_kubeconfig(self._kubeconfig_path)
        context_name = _required_string(document, "current-context", "current context")
        context_entry = _named_entry(document, "contexts", context_name)
        context_data = _required_mapping(context_entry, "context", "context data")
        cluster_name = _required_string(context_data, "cluster", "context cluster")
        cluster_entry = _named_entry(document, "clusters", cluster_name)
        cluster_data = _required_mapping(cluster_entry, "cluster", "cluster data")
        raw_server = _required_string(cluster_data, "server", "API Server")
        server = _normalize_server(raw_server)
        ca_sha256 = _ca_sha256(cluster_data, self._kubeconfig_path.parent)
        facts = self._cluster_probe.inspect(self._kubeconfig_path, context_name)
        return ContextIdentity(
            context_name=context_name,
            api_server=server,
            ca_sha256=ca_sha256,
            kube_system_uid=facts.kube_system_uid,
            kubernetes_version=facts.kubernetes_version,
        )


class MinikubeProfileVerifier:
    """Verify profile health and bind non-loopback servers to the exact minikube IP."""

    def __init__(self, locator: ToolFinder, runner: CommandRunner) -> None:
        self._locator = locator
        self._runner = runner

    def profile_for(self, identity: ContextIdentity) -> str | None:
        """Return the matching profile only when local minikube ownership is proven."""
        tool = self._locator.locate(ToolName.MINIKUBE)
        if tool is None:
            return None
        try:
            status = self._runner.run(
                tool.path,
                ["status", "--profile", identity.context_name, "--output=json"],
            )
        except ToolExecutionError:
            return None
        if status.returncode != 0 or not _minikube_running(status.stdout):
            return None

        hostname = urlsplit(identity.api_server).hostname
        if hostname is None:
            return None
        if _is_loopback(hostname):
            return identity.context_name

        try:
            result = self._runner.run(tool.path, ["ip", "--profile", identity.context_name])
        except ToolExecutionError:
            return None
        if result.returncode != 0 or not _same_ip(hostname, result.stdout.strip()):
            return None
        return identity.context_name


class ContextTrustService:
    """Manage trust records and provide the mandatory future write-operation guard."""

    def __init__(
        self,
        *,
        config: KubeLabConfig,
        config_path: Path,
        identity_provider: IdentityProvider,
        profile_verifier: ProfileVerifier,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._identity_provider = identity_provider
        self._profile_verifier = profile_verifier
        self._now = now

    def inspect(self) -> ContextInspection:
        """Inspect the current identity without changing local or cluster state."""
        identity = self._identity_provider.inspect_current()
        profile = self._profile_verifier.profile_for(identity)
        record = self._find_record(identity.context_name)
        state = self._state(identity, profile, record)
        return ContextInspection(
            **identity.model_dump(),
            minikube_profile=profile,
            trusted=state is TrustState.TRUSTED,
            trust_state=state,
        )

    def trust(self) -> TrustedContext:
        """Persist the current identity only after proving local minikube ownership."""
        identity = self._identity_provider.inspect_current()
        profile = self._profile_verifier.profile_for(identity)
        if profile is None:
            raise ContextNotLocalMinikubeError(
                "Current context is not a running local minikube profile."
            )
        record = TrustedContext(
            name=identity.context_name,
            server=identity.api_server,
            ca_sha256=identity.ca_sha256,
            kube_system_uid=identity.kube_system_uid,
            minikube_profile=profile,
            trusted_at=self._now(),
        )
        others = [item for item in self._config.trusted_contexts if item.name != record.name]
        self._config.trusted_contexts = [*others, record]
        save_config(self._config, self._config_path)
        return record

    def untrust(self) -> tuple[str, bool]:
        """Remove trust for the current context without contacting or changing the cluster."""
        context_name = self._identity_provider.current_context_name()
        remaining = [item for item in self._config.trusted_contexts if item.name != context_name]
        removed = len(remaining) != len(self._config.trusted_contexts)
        if removed:
            self._config.trusted_contexts = remaining
            save_config(self._config, self._config_path)
        return context_name, removed

    def assert_trusted_context(self) -> TrustedContext:
        """Reject every future cluster write unless the complete identity still matches."""
        context_name = self._identity_provider.current_context_name()
        record = self._find_record(context_name)
        if record is None:
            raise ContextNotTrustedError("Current context has not been explicitly trusted.")
        identity = self._identity_provider.inspect_current()
        profile = self._profile_verifier.profile_for(identity)
        if not _record_matches(identity, profile, record):
            raise ContextFingerprintMismatchError(
                "Current context identity no longer matches the trusted fingerprint."
            )
        return record

    def _find_record(self, context_name: str) -> TrustedContext | None:
        return next(
            (item for item in self._config.trusted_contexts if item.name == context_name),
            None,
        )

    @staticmethod
    def _state(
        identity: ContextIdentity,
        profile: str | None,
        record: TrustedContext | None,
    ) -> TrustState:
        if record is None:
            return TrustState.UNTRUSTED
        return (
            TrustState.TRUSTED if _record_matches(identity, profile, record) else TrustState.DRIFTED
        )


def build_context_trust_service() -> ContextTrustService:  # pragma: no cover
    """Build the production service against the explicit WSL kubeconfig."""
    config_path = get_config_path()
    local_config = load_config(config_path)
    identity_provider = KubeconfigIdentityProvider(
        resolve_kubeconfig_path(local_config), KubernetesClusterProbe()
    )
    return ContextTrustService(
        config=local_config,
        config_path=config_path,
        identity_provider=identity_provider,
        profile_verifier=MinikubeProfileVerifier(ToolLocator(local_config.tools), ProcessRunner()),
    )


def _record_matches(identity: ContextIdentity, profile: str | None, record: TrustedContext) -> bool:
    return (
        identity.context_name == record.name
        and identity.api_server == record.server
        and identity.ca_sha256 == record.ca_sha256
        and identity.kube_system_uid == record.kube_system_uid
        and profile == record.minikube_profile
    )


def _load_kubeconfig(path: Path) -> Mapping[str, object]:
    try:
        loaded = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ContextInspectionError("Unable to read the configured kubeconfig.") from exc
    return _as_mapping(loaded, "kubeconfig")


def _named_entry(document: Mapping[str, object], section: str, name: str) -> Mapping[str, object]:
    raw_entries = document.get(section)
    if not isinstance(raw_entries, list):
        raise ContextInspectionError(f"kubeconfig {section} is missing or invalid.")
    for raw_entry in raw_entries:
        entry = _as_mapping(raw_entry, f"{section} entry")
        if entry.get("name") == name:
            return entry
    raise ContextInspectionError(f"kubeconfig {section} does not contain the selected entry.")


def _required_mapping(document: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    return _as_mapping(document.get(key), label)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContextInspectionError(f"The configured {label} is missing or invalid.")
    return cast(dict[str, object], value)


def _required_string(document: Mapping[str, object], key: str, label: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ContextInspectionError(f"The configured {label} is missing or invalid.")
    return value


def _normalize_server(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ContextInspectionError("The configured API Server URL is invalid.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ContextInspectionError("The configured API Server URL is invalid.")
    hostname = parsed.hostname.lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}"


def _ca_sha256(cluster: Mapping[str, object], kubeconfig_dir: Path) -> str:
    encoded = cluster.get("certificate-authority-data")
    certificate_path = cluster.get("certificate-authority")
    try:
        if isinstance(encoded, str) and encoded:
            certificate = base64.b64decode(encoded, validate=True)
        elif isinstance(certificate_path, str) and certificate_path:
            candidate = Path(certificate_path).expanduser()
            resolved = candidate if candidate.is_absolute() else kubeconfig_dir / candidate
            certificate = resolved.read_bytes()
        else:
            raise ContextInspectionError("The selected cluster has no certificate authority.")
    except (OSError, ValueError, binascii.Error) as exc:
        raise ContextInspectionError(
            "Unable to fingerprint the cluster certificate authority."
        ) from exc
    if not certificate:
        raise ContextInspectionError("The cluster certificate authority is empty.")
    return hashlib.sha256(certificate).hexdigest()


def _minikube_running(output: str) -> bool:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return all(
        str(payload.get(component, "")).lower() == "running"
        for component in ("Host", "Kubelet", "APIServer")
    )


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _same_ip(first: str, second: str) -> bool:
    try:
        return ipaddress.ip_address(first) == ipaddress.ip_address(second)
    except ValueError:
        return False
