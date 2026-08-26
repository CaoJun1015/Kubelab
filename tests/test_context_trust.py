import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from kubelab.config import KubeLabConfig, TrustedContext, load_config
from kubelab.context_trust import (
    ClusterFacts,
    ContextFingerprintMismatchError,
    ContextIdentity,
    ContextInspectionError,
    ContextNotLocalMinikubeError,
    ContextNotTrustedError,
    ContextTrustService,
    KubeconfigIdentityProvider,
    MinikubeProfileVerifier,
    TrustState,
    trusted_context_fingerprint,
)
from kubelab.tools import (
    CommandResult,
    LocatedTool,
    ToolExecutionError,
    ToolSource,
)

NOW = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)
CA_SHA256 = "a" * 64


def identity(**updates: str) -> ContextIdentity:
    values = {
        "context_name": "minikube",
        "api_server": "https://127.0.0.1:32771",
        "ca_sha256": CA_SHA256,
        "kube_system_uid": "uid-kube-system",
        "kubernetes_version": "v1.35.1",
        **updates,
    }
    return ContextIdentity.model_validate(values)


def trusted_record(**updates: str) -> TrustedContext:
    values = {
        "name": "minikube",
        "server": "https://127.0.0.1:32771",
        "ca_sha256": CA_SHA256,
        "kube_system_uid": "uid-kube-system",
        "minikube_profile": "minikube",
        "trusted_at": NOW,
        **updates,
    }
    return TrustedContext.model_validate(values)


class FakeIdentityProvider:
    def __init__(self, current: ContextIdentity) -> None:
        self.current = current
        self.inspect_calls = 0

    def current_context_name(self) -> str:
        return self.current.context_name

    def inspect_current(self) -> ContextIdentity:
        self.inspect_calls += 1
        return self.current


class FakeProfileVerifier:
    def __init__(self, profile: str | None = "minikube") -> None:
        self.profile = profile

    def profile_for(self, current: ContextIdentity) -> str | None:
        del current
        return self.profile


def make_service(
    tmp_path: Path,
    *,
    current: ContextIdentity | None = None,
    profile: str | None = "minikube",
    records: list[TrustedContext] | None = None,
) -> tuple[ContextTrustService, KubeLabConfig, FakeIdentityProvider, Path]:
    config = KubeLabConfig(trusted_contexts=records or [])
    provider = FakeIdentityProvider(current or identity())
    config_path = tmp_path / "config.toml"
    service = ContextTrustService(
        config=config,
        config_path=config_path,
        identity_provider=provider,
        profile_verifier=FakeProfileVerifier(profile),
        now=lambda: NOW,
    )
    return service, config, provider, config_path


def test_inspect_reports_complete_untrusted_identity_without_writing(tmp_path: Path) -> None:
    service, _, _, config_path = make_service(tmp_path)

    result = service.inspect()

    assert result.context_name == "minikube"
    assert result.minikube_profile == "minikube"
    assert result.api_server == "https://127.0.0.1:32771"
    assert result.ca_sha256 == CA_SHA256
    assert result.kube_system_uid == "uid-kube-system"
    assert result.kubernetes_version == "v1.35.1"
    assert result.trusted is False
    assert result.trust_state is TrustState.UNTRUSTED
    assert not config_path.exists()


def test_trust_persists_identity_and_enables_write_guard(tmp_path: Path) -> None:
    service, _, _, config_path = make_service(tmp_path)

    record = service.trust()

    assert record.trusted_at == NOW
    persisted = load_config(config_path).trusted_contexts
    assert persisted == [record]
    assert service.inspect().trust_state is TrustState.TRUSTED
    assert service.assert_trusted_context() == record
    serialized = config_path.read_text(encoding="utf-8").lower()
    assert "token" not in serialized
    assert "certificate-authority-data" not in serialized


def test_retrust_replaces_same_named_fingerprint_without_duplicates(tmp_path: Path) -> None:
    old = trusted_record(server="https://127.0.0.1:11111")
    service, config, _, _ = make_service(tmp_path, records=[old])

    current = service.trust()

    assert config.trusted_contexts == [current]
    assert current.server == "https://127.0.0.1:32771"


def test_trusted_context_fingerprint_is_stable_and_covers_identity_fields() -> None:
    record = trusted_record()

    first = trusted_context_fingerprint(record)
    same_identity_new_time = trusted_record(trusted_at="2026-08-26T00:00:00Z")

    assert len(first) == 64
    assert first == trusted_context_fingerprint(same_identity_new_time)
    assert first != trusted_context_fingerprint(trusted_record(kube_system_uid="changed"))


def test_untrust_is_local_idempotent_and_does_not_probe_cluster(tmp_path: Path) -> None:
    service, _, provider, config_path = make_service(tmp_path, records=[trusted_record()])

    first = service.untrust()
    second = service.untrust()

    assert first == ("minikube", True)
    assert second == ("minikube", False)
    assert provider.inspect_calls == 0
    assert load_config(config_path).trusted_contexts == []


def test_write_guard_rejects_untrusted_context_and_context_switch(tmp_path: Path) -> None:
    service, _, provider, _ = make_service(tmp_path)
    with pytest.raises(ContextNotTrustedError, match="explicitly trusted"):
        service.assert_trusted_context()
    assert provider.inspect_calls == 0

    switched, _, switched_provider, _ = make_service(
        tmp_path,
        current=identity(context_name="another-context"),
        records=[trusted_record()],
    )
    with pytest.raises(ContextNotTrustedError, match="explicitly trusted"):
        switched.assert_trusted_context()
    assert switched_provider.inspect_calls == 0


@pytest.mark.parametrize(
    ("updates", "profile"),
    [
        ({"api_server": "https://127.0.0.1:44444"}, "minikube"),
        ({"ca_sha256": "b" * 64}, "minikube"),
        ({"kube_system_uid": "different-uid"}, "minikube"),
        ({}, "different-profile"),
        ({}, None),
    ],
)
def test_write_guard_rejects_every_fingerprint_drift(
    tmp_path: Path, updates: dict[str, str], profile: str | None
) -> None:
    service, _, _, _ = make_service(
        tmp_path,
        current=identity(**updates),
        profile=profile,
        records=[trusted_record()],
    )

    assert service.inspect().trust_state is TrustState.DRIFTED
    with pytest.raises(ContextFingerprintMismatchError, match="no longer matches"):
        service.assert_trusted_context()


def test_trust_rejects_non_minikube_or_unproven_remote_context(tmp_path: Path) -> None:
    service, _, _, config_path = make_service(
        tmp_path,
        current=identity(
            context_name="production",
            api_server="https://kubernetes.example.com:6443",
        ),
        profile=None,
    )

    with pytest.raises(ContextNotLocalMinikubeError, match="local minikube"):
        service.trust()

    assert not config_path.exists()


class FakeClusterProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def inspect(self, kubeconfig_path: Path, context_name: str) -> ClusterFacts:
        self.calls.append((kubeconfig_path, context_name))
        return ClusterFacts(kube_system_uid="api-uid", kubernetes_version="v1.35.1")


def write_kubeconfig(
    path: Path,
    *,
    server: str = "https://LOCALHOST:8443/",
    ca_data: str | None = None,
    ca_path: str | None = None,
) -> bytes:
    certificate = b"test-ca-certificate"
    cluster: dict[str, str] = {"server": server}
    cluster["certificate-authority-data"] = ca_data or base64.b64encode(certificate).decode()
    if ca_path is not None:
        cluster.pop("certificate-authority-data", None)
        cluster["certificate-authority"] = ca_path
    document = {
        "apiVersion": "v1",
        "current-context": "minikube",
        "contexts": [{"name": "minikube", "context": {"cluster": "minikube"}}],
        "clusters": [{"name": "minikube", "cluster": cluster}],
        "users": [
            {
                "name": "private-user",
                "user": {"token": "must-never-appear", "client-key-data": "private-key"},
            }
        ],
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return certificate


def test_kubeconfig_provider_hashes_inline_ca_and_ignores_credentials(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "config"
    certificate = write_kubeconfig(kubeconfig)
    probe = FakeClusterProbe()
    provider = KubeconfigIdentityProvider(kubeconfig, probe)

    result = provider.inspect_current()

    assert provider.current_context_name() == "minikube"
    assert result.api_server == "https://localhost:8443"
    assert result.ca_sha256 == hashlib.sha256(certificate).hexdigest()
    assert result.kube_system_uid == "api-uid"
    assert probe.calls == [(kubeconfig, "minikube")]
    public_json = result.model_dump_json()
    assert "must-never-appear" not in public_json
    assert "private-key" not in public_json
    assert base64.b64encode(certificate).decode() not in public_json


def test_kubeconfig_provider_hashes_relative_ca_file(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "config"
    certificate = b"certificate-from-file"
    (tmp_path / "minikube-ca.crt").write_bytes(certificate)
    write_kubeconfig(kubeconfig, ca_path="minikube-ca.crt")

    result = KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe()).inspect_current()

    assert result.ca_sha256 == hashlib.sha256(certificate).hexdigest()


@pytest.mark.parametrize(
    "server",
    [
        "https://user:password@127.0.0.1:8443",
        "https://127.0.0.1:8443/path?token=secret",
        "http://127.0.0.1:8080",
        "not-a-url",
        "https://127.0.0.1:invalid",
    ],
)
def test_kubeconfig_provider_rejects_credentialed_or_invalid_server_without_echo(
    tmp_path: Path, server: str
) -> None:
    kubeconfig = tmp_path / "config"
    write_kubeconfig(kubeconfig, server=server)

    with pytest.raises(ContextInspectionError) as captured:
        KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe()).inspect_current()

    message = str(captured.value).lower()
    assert "password" not in message
    assert "token" not in message
    assert "secret" not in message


def test_kubeconfig_provider_rejects_invalid_ca_and_missing_context(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "config"
    write_kubeconfig(kubeconfig, ca_data="not-base64!")
    with pytest.raises(ContextInspectionError, match="fingerprint"):
        KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe()).inspect_current()

    kubeconfig.write_text("apiVersion: v1\ncontexts: []\n", encoding="utf-8")
    provider = KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe())
    with pytest.raises(ContextInspectionError, match="current context"):
        provider.current_context_name()


def test_kubeconfig_provider_rejects_malformed_or_missing_entries(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("contexts: [", encoding="utf-8")
    with pytest.raises(ContextInspectionError, match="Unable to read"):
        KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe()).current_context_name()

    kubeconfig.write_text(
        yaml.safe_dump({"current-context": "missing", "contexts": []}),
        encoding="utf-8",
    )
    with pytest.raises(ContextInspectionError, match="selected entry"):
        KubeconfigIdentityProvider(kubeconfig, FakeClusterProbe()).inspect_current()


class FakeToolFinder:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def locate(self, name) -> LocatedTool | None:
        if not self.available:
            return None
        return LocatedTool(name, Path("/usr/local/bin/minikube"), ToolSource.PATH)


class FakeCommandRunner:
    def __init__(
        self,
        *,
        status: str = '{"Host":"Running","Kubelet":"Running","APIServer":"Running"}',
        status_code: int = 0,
        minikube_ip: str = "192.168.49.2",
        fail: bool = False,
    ) -> None:
        self.status = status
        self.status_code = status_code
        self.minikube_ip = minikube_ip
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        executable: Path,
        arguments: Sequence[str],
        *,
        timeout_seconds: int = 10,
    ) -> CommandResult:
        del timeout_seconds
        self.calls.append(tuple(arguments))
        if self.fail:
            raise ToolExecutionError("safe failure")
        stdout = self.status if arguments[0] == "status" else self.minikube_ip
        return CommandResult(
            args=(str(executable), *arguments),
            returncode=self.status_code if arguments[0] == "status" else 0,
            stdout=stdout,
            stderr="",
        )


def test_minikube_verifier_accepts_running_loopback_without_ip_lookup() -> None:
    runner = FakeCommandRunner()
    verifier = MinikubeProfileVerifier(FakeToolFinder(), runner)

    assert verifier.profile_for(identity()) == "minikube"
    assert len(runner.calls) == 1
    assert runner.calls[0] == ("status", "--profile", "minikube", "--output=json")


def test_minikube_verifier_accepts_only_exact_non_loopback_profile_ip() -> None:
    matching = MinikubeProfileVerifier(FakeToolFinder(), FakeCommandRunner())
    remote_identity = identity(api_server="https://192.168.49.2:8443")
    assert matching.profile_for(remote_identity) == "minikube"

    mismatched = MinikubeProfileVerifier(
        FakeToolFinder(), FakeCommandRunner(minikube_ip="192.168.49.3")
    )
    assert mismatched.profile_for(remote_identity) is None


@pytest.mark.parametrize(
    ("finder", "runner"),
    [
        (FakeToolFinder(False), FakeCommandRunner()),
        (FakeToolFinder(), FakeCommandRunner(status_code=7)),
        (FakeToolFinder(), FakeCommandRunner(status="not-json")),
        (FakeToolFinder(), FakeCommandRunner(status='{"Host":"Stopped"}')),
        (FakeToolFinder(), FakeCommandRunner(fail=True)),
    ],
)
def test_minikube_verifier_rejects_missing_stopped_or_invalid_profile(
    finder: FakeToolFinder, runner: FakeCommandRunner
) -> None:
    assert MinikubeProfileVerifier(finder, runner).profile_for(identity()) is None
