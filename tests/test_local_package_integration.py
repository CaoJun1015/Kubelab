"""Opt-in WSL integration for an imported local package's full learner lifecycle."""

from __future__ import annotations

import os
import platform
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from kubelab.authoring import AuthoringService
from kubelab.authoring_integration import _integration_prerequisite
from kubelab.config import TrustedContext, load_config, resolve_kubeconfig_path
from kubelab.context_trust import build_context_trust_service, trusted_context_fingerprint
from kubelab.database import Database
from kubelab.doctor import build_doctor_service
from kubelab.guided_learning import EnvironmentReadinessService
from kubelab.kubernetes_gateway import KubernetesGateway, SessionScope
from kubelab.lab_manager import LabManager
from kubelab.lab_registry import LabRegistry
from kubelab.manifest_security import ManifestDocument
from kubelab.operation_lock import OperationLock
from kubelab.package_catalog import PackageCatalogRegistry
from kubelab.package_manager import PackageManager
from kubelab.session_state import LabSessionSnapshot, ValidationStatus
from kubelab.validation_engine import ValidationEngine

pytestmark = pytest.mark.integration


def test_imported_package_runs_pinned_lifecycle_and_leaves_no_owned_residue() -> None:
    """Exercise package -> catalog -> Session -> repair -> cleanup when explicitly enabled."""
    _require_package_integration()
    with tempfile.TemporaryDirectory(prefix="kubelab-package-integration-") as temporary_name:
        root = Path(temporary_name)
        author_workspace = root / "author"
        author_workspace.mkdir()
        family = author_workspace / "lab-local-package-integration"
        authoring = AuthoringService(author_workspace)
        initialized = authoring.init(
            family,
            scenario_type="baseline",
            scenario_id="lab-local-package-integration",
            title="本地包真实闭环",
            category="workload",
            difficulty="intermediate",
            description="验证可信本地实验包的真实运行边界。",
        )
        assert initialized.passed, initialized.issues
        archive = author_workspace / "lab-local-package-integration-0.1.0.kubelab-lab.tar.gz"
        packaged = authoring.package(family, output=archive)
        assert packaged.passed, packaged.issues

        state = root / "state"
        database = Database(state / "kubelab.db")
        database.initialize()
        builtin = LabRegistry()
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=state / "packages",
            builtin_registry=builtin,
            operation_lock=OperationLock(state / "package.lock"),
        )
        imported = packages.import_archive(archive)
        packages.enable("lab-local-package-integration", "0.1.0")
        catalog = PackageCatalogRegistry(
            builtin_registry=builtin,
            unit_of_work=database.unit_of_work,
            state_root=state / "packages",
        )
        trust = build_context_trust_service()
        record = trust.assert_trusted_context()
        fingerprint = trusted_context_fingerprint(record)
        kubeconfig = resolve_kubeconfig_path(load_config())

        def gateway_factory(
            trusted: TrustedContext,
            context_fingerprint: str,
        ) -> KubernetesGateway:
            del trusted, context_fingerprint
            return KubernetesGateway.from_kubeconfig(
                kubeconfig_path=kubeconfig,
                context_name=record.name,
                context_fingerprint=fingerprint,
            )

        manager = LabManager(
            registry=catalog,
            unit_of_work=database.unit_of_work,
            operation_lock=OperationLock(state / "session.lock"),
            context_trust=trust,
            gateway_factory=gateway_factory,
            validation=ValidationEngine(database.unit_of_work),
            readiness=EnvironmentReadinessService(
                doctor=build_doctor_service(),
                context_trust=trust,
                unit_of_work=database.unit_of_work,
            ),
        )
        session: LabSessionSnapshot | None = None
        try:
            session = manager.start("lab-local-package-integration")
            assert session.package_sha256 == imported.package.sha256
            _apply_packaged_repair(
                catalog=catalog,
                packages_root=state / "packages",
                session=session,
                gateway_factory=gateway_factory,
                trusted=record,
                fingerprint=fingerprint,
            )
            result = manager.verify(session.id)
            assert result.status is ValidationStatus.PASSED
            completed = manager.cleanup(session.id)
            assert completed.status.value == "completed"
            removed = packages.remove("lab-local-package-integration", "0.1.0")
            assert removed.package.status.value == "removed"

            audit = gateway_factory(record, fingerprint)
            try:
                assert not audit.namespace_exists(_scope(session))
                assert not audit.authoring_persistent_volume_residue(_scope(session))
            finally:
                audit.close()
        finally:
            if session is not None:
                try:
                    manager.cleanup(session.id)
                except Exception:
                    pass
            database.dispose()


def _require_package_integration() -> None:
    if os.environ.get("KUBELAB_RUN_PACKAGE_INTEGRATION") != "1":
        pytest.skip("Set KUBELAB_RUN_PACKAGE_INTEGRATION=1 to run the local package lifecycle")
    if platform.system() != "Linux" or not os.environ.get(
        "WSL_DISTRO_NAME", ""
    ).casefold().startswith("ubuntu"):
        pytest.fail("Package integration is restricted to WSL2 Ubuntu")
    with tempfile.TemporaryDirectory(prefix="kubelab-package-gate-") as temporary_name:
        service = AuthoringService(Path(temporary_name))
        issue = _integration_prerequisite(service)
    if issue is not None:
        pytest.fail(f"{issue.code}: {issue.message}")


def _apply_packaged_repair(
    *,
    catalog: PackageCatalogRegistry,
    packages_root: Path,
    session: LabSessionSnapshot,
    gateway_factory: Any,
    trusted: TrustedContext,
    fingerprint: str,
) -> None:
    loaded = catalog.pinned_lab(session.lab_id, session.package_sha256)
    assert loaded is not None
    content = packages_root / "blobs" / str(session.package_sha256) / "content"
    authoring = AuthoringService(content)
    family = content.joinpath(*Path(loaded.lab_path).parts)
    target = authoring._load_target(family)
    assert not target.issues and len(target.scenarios) == 1
    scenario = target.scenarios[0]
    repair = scenario.contract.repairs.full
    repair_path = scenario.directory.joinpath(*PurePosixPath(repair.manifest).parts)
    documents, issue = authoring._load_manifest_documents(repair_path)
    assert issue is None
    rewritten = tuple(_rewrite_namespace(item, session.namespace) for item in documents)
    recreate = frozenset(
        (change.resource.api_version, change.resource.kind, change.resource.name)
        for change in repair.allowed_changes
        if change.operation == "recreate"
    )
    gateway = gateway_factory(trusted, fingerprint)
    try:
        gateway.apply_authoring_repair(_scope(session), rewritten, recreate=recreate)
    finally:
        gateway.close()


def _scope(session: LabSessionSnapshot) -> SessionScope:
    return SessionScope(
        lab_id=session.lab_id,
        session_id=session.id,
        namespace=session.namespace,
        context_fingerprint=session.context_fingerprint,
    )


def _rewrite_namespace(document: ManifestDocument, namespace: str) -> ManifestDocument:
    value = dict(document.data)
    metadata_value = value.get("metadata")
    assert isinstance(metadata_value, Mapping)
    metadata = dict(metadata_value)
    metadata["namespace"] = namespace
    value["metadata"] = metadata
    return ManifestDocument(
        manifest_path=document.manifest_path,
        document_index=document.document_index,
        data=value,
    )
