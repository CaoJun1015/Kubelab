"""Application-service tests for local package inventory and lifecycle rules."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from kubelab.authoring import AuthoringService
from kubelab.database import Database
from kubelab.lab_registry import LabRegistry
from kubelab.package_manager import PackageIntegrity, PackageManager, PackageManagerError
from kubelab.package_state import LabSource, PackageStatus
from kubelab.session_state import NewLabSession, SessionStatus

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"


def _database(tmp_path: Path) -> Database:
    database = Database(
        tmp_path / "state" / "kubelab.db",
        lock_path=tmp_path / "state" / "operations.lock",
        lock_timeout_seconds=0,
    )
    database.initialize()
    return database


def _manager(tmp_path: Path, database: Database, *, supported: bool = True) -> PackageManager:
    return PackageManager(
        unit_of_work=database.unit_of_work,
        state_root=tmp_path / "packages",
        builtin_registry=LabRegistry(LABS_ROOT),
        platform_supported=lambda: supported,
    )


def _archive(
    tmp_path: Path,
    *,
    lab_id: str = "lab-local-networking",
    version: str = "1.0.0",
    publisher_id: str = "local-author",
    publisher_name: str = "Local Author",
) -> Path:
    publisher_label = publisher_name.casefold().replace(" ", "-")
    workspace = tmp_path / f"author-{lab_id}-{version}-{publisher_id}-{publisher_label}"
    workspace.mkdir()
    family = workspace / lab_id
    service = AuthoringService(workspace)
    initialized = service.init(
        family,
        scenario_type="baseline",
        scenario_id=lab_id,
        title="本地网络实验",
        category="networking",
        difficulty="intermediate",
        description="用于验证本地包生命周期。",
    )
    assert initialized.passed
    metadata_path = family / "package.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["metadata"]["version"] = version
    metadata["metadata"]["publisherId"] = publisher_id
    metadata["metadata"]["publisherName"] = publisher_name
    metadata_path.write_text(
        yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    output = workspace / f"{lab_id}-{version}.kubelab-lab.tar.gz"
    packaged = service.package(family, output=output)
    assert packaged.passed, packaged.issues
    return output


def _legacy_archive(source: Path, destination: Path) -> Path:
    with tarfile.open(source, "r:gz") as archive:
        contents = {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
        }
    index = json.loads(contents["index.json"])
    index["formatVersion"] = 1
    index.pop("package")
    index["schemaVersions"].pop("package")
    contents["index.json"] = json.dumps(index).encode()
    with tarfile.open(destination, "w:gz") as archive:
        for name, content in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return destination


def test_verify_is_cross_platform_and_legacy_archives_are_not_importable(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        archive = _archive(tmp_path)
        manager = _manager(tmp_path, database, supported=False)

        verified = manager.verify(archive)
        legacy = manager.verify(_legacy_archive(archive, tmp_path / "legacy.kubelab-lab.tar.gz"))

        assert verified.importable
        assert verified.publisher_verified is False
        assert verified.scenario_count == 1
        assert not legacy.importable
        with pytest.raises(PackageManagerError) as blocked:
            manager.import_archive(archive)
        assert blocked.value.code == "PACKAGE_PLATFORM_UNSUPPORTED"
        assert blocked.value.exit_code == 5
    finally:
        database.dispose()


def test_import_is_staged_idempotent_then_enable_disable_and_remove(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        archive = _archive(tmp_path)
        manager = _manager(tmp_path, database)

        imported = manager.import_archive(archive)
        repeated = manager.import_archive(archive)
        enabled = manager.enable("lab-local-networking", "1.0.0")
        disabled = manager.disable("lab-local-networking")
        removed = manager.remove("lab-local-networking", "1.0.0")

        assert imported.package.status is PackageStatus.STAGED
        assert imported.package.integrity is PackageIntegrity.VERIFIED
        assert not imported.package.available
        assert repeated.idempotent
        assert enabled.package.status is PackageStatus.ENABLED
        assert enabled.package.available
        assert disabled.package.status is PackageStatus.DISABLED
        assert removed.package.status is PackageStatus.REMOVED
        assert removed.package.integrity is PackageIntegrity.INVALID
        assert manager.show("lab-local-networking", "1.0.0") == (removed.package,)
        assert not (tmp_path / "packages" / "blobs" / imported.package.sha256).exists()
    finally:
        database.dispose()


def test_version_switch_and_rollback_leave_one_enabled_version(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        first = _archive(tmp_path, version="1.0.0")
        second = _archive(tmp_path, version="1.1.0")
        manager = _manager(tmp_path, database)
        manager.import_archive(first)
        manager.import_archive(second)

        v1 = manager.enable("lab-local-networking", "1.0.0")
        v2 = manager.enable("lab-local-networking", "1.1.0")
        rolled_back = manager.enable("lab-local-networking", "1.0.0")
        records = manager.show("lab-local-networking")

        assert v1.package.sha256 != v2.package.sha256
        assert rolled_back.package.sha256 == v1.package.sha256
        assert [item.status for item in records].count(PackageStatus.ENABLED) == 1
        assert next(
            item for item in records if item.status is PackageStatus.ENABLED
        ).package_version == ("1.0.0")
    finally:
        database.dispose()


def test_builtin_id_publisher_lock_and_version_content_conflict_are_rejected(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        reserved = _archive(tmp_path, lab_id="lab-001-deployment-scaling")
        with pytest.raises(PackageManagerError) as builtin:
            manager.import_archive(reserved)
        assert builtin.value.code == "PACKAGE_BUILTIN_ID_RESERVED"

        original = _archive(tmp_path, version="1.0.0")
        manager.import_archive(original)
        conflicting = _archive(
            tmp_path,
            version="1.0.0",
            publisher_name="Renamed Publisher",
        )
        with pytest.raises(PackageManagerError) as version_conflict:
            manager.import_archive(conflicting)
        assert version_conflict.value.code == "PACKAGE_VERSION_CONFLICT"

        another_publisher = _archive(
            tmp_path,
            version="2.0.0",
            publisher_id="another-author",
        )
        with pytest.raises(PackageManagerError) as publisher_conflict:
            manager.import_archive(another_publisher)
        assert publisher_conflict.value.code == "PACKAGE_PUBLISHER_CONFLICT"
    finally:
        database.dispose()


def test_active_session_defers_removal_and_completed_session_allows_sweep(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        imported = manager.import_archive(_archive(tmp_path))
        manager.enable("lab-local-networking", "1.0.0")
        with database.unit_of_work() as uow:
            session = uow.sessions.create(
                NewLabSession(
                    id="00000000-0000-4000-8000-000000000909",
                    lab_id="lab-local-networking",
                    lab_source=LabSource.LOCAL_PACKAGE,
                    package_sha256=imported.package.sha256,
                    lab_public_snapshot={"name": "Local networking"},
                    scenario_public_snapshot={"revealed": False},
                    namespace="kubelab-local-networking",
                    context_name="minikube",
                    context_fingerprint="d" * 64,
                )
            )
            uow.commit()

        deferred = manager.remove("lab-local-networking", "1.0.0")

        assert deferred.deferred
        assert deferred.package.status is PackageStatus.PENDING_REMOVAL
        blob = tmp_path / "packages" / "blobs" / imported.package.sha256
        assert blob.is_dir()
        with database.unit_of_work() as uow:
            uow.sessions.transition(
                session.id,
                SessionStatus.CLEANING,
                event_type="cleanup_started",
            )
            uow.sessions.transition(
                session.id,
                SessionStatus.COMPLETED,
                event_type="cleanup_completed",
            )
            uow.commit()

        records = manager.list_packages()

        assert records[0].status is PackageStatus.REMOVED
        assert not blob.exists()
    finally:
        database.dispose()


def test_stored_archive_damage_is_visible_and_blocks_activation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        imported = manager.import_archive(_archive(tmp_path))
        stored = (
            tmp_path / "packages" / "blobs" / imported.package.sha256 / "archive.kubelab-lab.tar.gz"
        )
        stored.write_bytes(b"damaged")

        listed = manager.list_packages()

        assert listed[0].integrity is PackageIntegrity.INVALID
        assert not listed[0].available
        with pytest.raises(PackageManagerError) as blocked:
            manager.enable("lab-local-networking", "1.0.0")
        assert blocked.value.code in {"PACKAGE_ARCHIVE_INVALID", "PACKAGE_STORED_DIGEST_MISMATCH"}
    finally:
        database.dispose()


def test_format_v1_import_returns_stable_compatibility_error(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        source = _archive(tmp_path)
        legacy = _legacy_archive(source, tmp_path / "legacy-import.kubelab-lab.tar.gz")
        manager = _manager(tmp_path, database)

        with pytest.raises(PackageManagerError) as caught:
            manager.import_archive(legacy)

        assert caught.value.code == "PACKAGE_FORMAT_NOT_IMPORTABLE"
        assert caught.value.exit_code == 2
        assert manager.list_packages() == ()
    finally:
        database.dispose()
