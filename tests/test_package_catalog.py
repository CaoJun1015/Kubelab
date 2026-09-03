"""Enabled-package catalogue composition and Session digest pinning."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from kubelab.authoring import AuthoringService
from kubelab.config import TrustedContext
from kubelab.context_trust import trusted_context_fingerprint
from kubelab.database import Database
from kubelab.kubernetes_gateway import NamespaceDeleteResult, SessionScope
from kubelab.lab_manager import InitialContractResult, LabManager, LabManagerError
from kubelab.lab_registry import ExecutableLab, LabRegistry
from kubelab.operation_lock import OperationLock
from kubelab.package_catalog import PackageCatalogRegistry
from kubelab.package_manager import PackageManager
from kubelab.package_state import LabSource, NewLabPackage, PackageStatus
from kubelab.session_state import ValidationStatus

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"


def _archive(tmp_path: Path, version: str) -> Path:
    workspace = tmp_path / f"author-{version}"
    workspace.mkdir()
    family = workspace / "lab-local-catalog"
    service = AuthoringService(workspace)
    initialized = service.init(
        family,
        scenario_type="baseline",
        scenario_id="lab-local-catalog",
        title=f"本地目录实验 {version}",
        category="networking",
        difficulty="intermediate",
        description="验证本地包目录与Session钉住。",
    )
    assert initialized.passed
    package_path = family / "package.yaml"
    package = yaml.safe_load(package_path.read_text(encoding="utf-8"))
    package["metadata"]["version"] = version
    package_path.write_text(
        yaml.safe_dump(package, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    output = workspace / f"lab-local-catalog-{version}.kubelab-lab.tar.gz"
    result = service.package(family, output=output)
    assert result.passed, result.issues
    return output


class _Trust:
    def __init__(self) -> None:
        self.record = TrustedContext(
            name="minikube",
            server="https://127.0.0.1:32771",
            ca_sha256="a" * 64,
            kube_system_uid="local-cluster",
            minikube_profile="minikube",
            trusted_at=datetime(2026, 9, 2, tzinfo=UTC),
        )

    def assert_trusted_context(self) -> TrustedContext:
        return self.record


class _Validation:
    def validate_initial_contract(
        self,
        scope: SessionScope,
        lab: ExecutableLab,
        gateway: Any,
        reset_sequence: int,
    ) -> InitialContractResult:
        del scope, lab, gateway, reset_sequence
        return InitialContractResult(status=ValidationStatus.PASSED)

    def validate_success_contract(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("success validation is not used in this test")


class _Gateway:
    def __init__(self) -> None:
        self.applied_digests: list[str | None] = []

    def create_environment(self, scope: SessionScope) -> None:
        del scope

    def apply_lab(
        self,
        scope: SessionScope,
        loaded: ExecutableLab,
        registry: LabRegistry,
    ) -> None:
        del scope
        registry.materialize_for_gateway(loaded)
        parent = loaded.parent if hasattr(loaded, "parent") else loaded
        self.applied_digests.append(parent.package_sha256)

    def delete_environment(
        self,
        scope: SessionScope,
        *,
        wait_timeout_seconds: float = 120,
    ) -> NamespaceDeleteResult:
        del wait_timeout_seconds
        return NamespaceDeleteResult(
            namespace=scope.namespace,
            deleted=True,
            already_absent=False,
        )

    def list_resources(self, scope: SessionScope) -> tuple[()]:
        del scope
        return ()

    def list_pods(self, scope: SessionScope) -> tuple[()]:
        del scope
        return ()

    def close(self) -> None:
        return None


def test_catalog_only_exposes_enabled_version_and_resolves_pinned_digest(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    try:
        builtin = LabRegistry(LABS_ROOT)
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
            builtin_registry=builtin,
            platform_supported=lambda: True,
        )
        first = packages.import_archive(_archive(tmp_path, "1.0.0"))
        second = packages.import_archive(_archive(tmp_path, "1.1.0"))
        packages.enable("lab-local-catalog", "1.0.0")
        catalog = PackageCatalogRegistry(
            builtin_registry=builtin,
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
        )

        initial = catalog.scan()

        assert len(initial.labs) == 22
        local = next(
            item for item in initial.labs if item.definition.metadata.id == "lab-local-catalog"
        )
        assert local.source is LabSource.LOCAL_PACKAGE
        assert local.package_sha256 == first.package.sha256
        assert local.package_version == "1.0.0"

        packages.enable("lab-local-catalog", "1.1.0")
        current = next(
            item
            for item in catalog.scan().labs
            if item.definition.metadata.id == "lab-local-catalog"
        )
        pinned = catalog.pinned_lab("lab-local-catalog", first.package.sha256)

        assert current.package_sha256 == second.package.sha256
        assert current.package_version == "1.1.0"
        assert pinned is not None and pinned.package_version == "1.0.0"
    finally:
        database.dispose()


def test_lab_manager_pins_session_and_history_survives_package_removal(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    try:
        builtin = LabRegistry(LABS_ROOT)
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
            builtin_registry=builtin,
            platform_supported=lambda: True,
        )
        imported = packages.import_archive(_archive(tmp_path, "1.0.0"))
        packages.enable("lab-local-catalog", "1.0.0")
        catalog = PackageCatalogRegistry(
            builtin_registry=builtin,
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
        )
        trust = _Trust()
        gateway = _Gateway()
        manager = LabManager(
            registry=catalog,
            unit_of_work=database.unit_of_work,
            operation_lock=OperationLock(tmp_path / "manager.lock", timeout_seconds=0),
            context_trust=trust,  # type: ignore[arg-type]
            gateway_factory=lambda trusted, fingerprint: gateway,
            validation=_Validation(),  # type: ignore[arg-type]
            session_completed_hook=packages.sweep_pending_removals,
        )

        detail = manager.show_lab("lab-local-catalog")

        assert detail.lab.source is LabSource.LOCAL_PACKAGE
        assert detail.lab.package_version == "1.0.0"
        assert detail.lab.publisher_id == "local-author"
        assert detail.lab.publisher_name == "Local Author"
        assert not detail.lab.publisher_verified
        assert detail.lab.available

        session = manager.start("lab-local-catalog")

        assert session.lab_source is LabSource.LOCAL_PACKAGE
        assert session.package_sha256 == imported.package.sha256
        assert session.lab_public_snapshot is not None
        assert session.lab_public_snapshot["package_version"] == "1.0.0"
        assert gateway.applied_digests == [imported.package.sha256]
        packages.disable("lab-local-catalog")
        hint = manager.next_hint(session.id)
        assert hint.level == 1

        stored_archive = (
            tmp_path / "packages" / "blobs" / imported.package.sha256 / "archive.kubelab-lab.tar.gz"
        )
        stored_archive.write_bytes(b"damaged")
        restored = manager.session_status_snapshot(session.id)
        assert not restored.session.available
        with pytest.raises(LabManagerError) as unavailable:
            manager.next_hint(session.id)
        assert unavailable.value.code == "LAB_PACKAGE_UNAVAILABLE"

        deferred = packages.remove("lab-local-catalog", "1.0.0")
        assert deferred.package.status is PackageStatus.PENDING_REMOVAL
        assert deferred.deferred
        completed = manager.cleanup(session.id)
        assert completed.status.value == "completed"
        with database.unit_of_work() as uow:
            assert uow.packages.require(imported.package.sha256).status is PackageStatus.REMOVED
        assert packages.list_packages()[0].status is PackageStatus.REMOVED

        retrospective = manager.retrospective(session.id)
        progress = manager.progress()
        historical = next(item for item in progress.labs if item.lab_id == "lab-local-catalog")

        assert retrospective.metadata is not None
        assert retrospective.metadata.lab_name == "本地目录实验 1.0.0"
        assert retrospective.metadata.package_version == "1.0.0"
        assert not retrospective.metadata.available
        assert historical.source is LabSource.LOCAL_PACKAGE
        assert historical.package_version == "1.0.0"
        assert not historical.available
        assert trusted_context_fingerprint(trust.record) == session.context_fingerprint
    finally:
        database.dispose()


def test_catalog_isolates_damaged_enabled_package_and_preserves_builtins(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    try:
        builtin = LabRegistry(LABS_ROOT)
        packages = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
            builtin_registry=builtin,
            platform_supported=lambda: True,
        )
        imported = packages.import_archive(_archive(tmp_path, "1.0.0"))
        packages.enable("lab-local-catalog", "1.0.0")
        content = tmp_path / "packages" / "blobs" / imported.package.sha256 / "content"
        stored_lab = next(content.rglob("lab.yaml"))
        stored_lab.write_text("tampered", encoding="utf-8")
        catalog = PackageCatalogRegistry(
            builtin_registry=builtin,
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
        )

        snapshot = catalog.scan()

        assert packages.list_packages()[0].integrity.value == "invalid"
        assert len(snapshot.labs) == 21
        assert {item.definition.metadata.id for item in snapshot.labs} == {
            item.definition.metadata.id for item in builtin.scan().labs
        }
        assert any(error.lab_id == "lab-local-catalog" for error in snapshot.errors)
        assert catalog.pinned_lab("lab-local-catalog", imported.package.sha256) is None
        assert catalog.pinned_lab("lab-001-deployment-scaling") is not None
    finally:
        database.dispose()


def test_catalog_never_composes_a_local_record_over_a_builtin_id(tmp_path: Path) -> None:
    database = Database(tmp_path / "state" / "kubelab.db")
    database.initialize()
    try:
        builtin = LabRegistry(LABS_ROOT)
        digest = "b" * 64
        with database.unit_of_work() as uow:
            uow.packages.add(
                NewLabPackage(
                    sha256=digest,
                    lab_id="lab-001-deployment-scaling",
                    package_version="9.9.9",
                    publisher_id="local-author",
                    publisher_name="Local Author",
                    requires_kubelab=">=0.6.0a0,<0.7.0",
                    format_version=2,
                    archive_size=1,
                )
            )
            uow.packages.set_status(
                digest,
                PackageStatus.ENABLED,
                event_type="enabled",
            )
            uow.commit()
        catalog = PackageCatalogRegistry(
            builtin_registry=builtin,
            unit_of_work=database.unit_of_work,
            state_root=tmp_path / "packages",
        )

        snapshot = catalog.scan()

        assert len(snapshot.labs) == 21
        assert (
            sum(
                item.definition.metadata.id == "lab-001-deployment-scaling"
                for item in snapshot.labs
            )
            == 1
        )
        assert any(error.lab_id == "lab-001-deployment-scaling" for error in snapshot.errors)
    finally:
        database.dispose()
