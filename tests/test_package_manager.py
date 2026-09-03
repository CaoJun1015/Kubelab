"""Application-service tests for local package inventory and lifecycle rules."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from kubelab import cli
from kubelab import package_manager as package_manager_module
from kubelab.authoring import AuthoringService
from kubelab.cli import app
from kubelab.database import Database
from kubelab.lab_registry import LabRegistry
from kubelab.package_manager import (
    PackageInfo,
    PackageIntegrity,
    PackageManager,
    PackageManagerError,
    PackageOperation,
    verify_package_archive,
)
from kubelab.package_state import LabSource, PackageStatus
from kubelab.session_state import NewLabSession, SessionStatus

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"
RUNNER = CliRunner()


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
        stored_bytes = manager._stored_bytes()
        repeated = manager.import_archive(archive)
        enabled = manager.enable("lab-local-networking", "1.0.0")
        disabled = manager.disable("lab-local-networking")
        removed = manager.remove("lab-local-networking", "1.0.0")

        assert imported.package.status is PackageStatus.STAGED
        assert stored_bytes > archive.stat().st_size
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
        archive = _archive(tmp_path)
        imported = manager.import_archive(archive)
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

        manager.sweep_pending_removals()
        records = manager.list_packages()

        assert records[0].status is PackageStatus.REMOVED
        assert not blob.exists()
    finally:
        database.dispose()


def test_stored_archive_damage_is_visible_and_blocks_activation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        archive = _archive(tmp_path)
        imported = manager.import_archive(archive)
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
        with pytest.raises(PackageManagerError) as reimported:
            manager.import_archive(archive)
        assert reimported.value.code == "PACKAGE_STORED_CONTENT_INVALID"
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


def test_package_verify_cli_is_cross_platform_json_without_absolute_paths(tmp_path: Path) -> None:
    archive = _archive(tmp_path)

    result = RUNNER.invoke(app, ["package", "verify", str(archive), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["filename"] == archive.name
    assert payload["formatVersion"] == 2
    assert payload["publisherVerified"] is False
    assert str(tmp_path) not in result.stdout


def test_package_management_cli_json_and_removal_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    info = PackageInfo(
        labId="lab-local-networking",
        packageVersion="1.0.0",
        publisherId="local-author",
        publisherName="Local Author",
        publisherVerified=False,
        requiresKubelab=">=0.6.0a0,<0.7.0",
        formatVersion=2,
        archiveSize=1024,
        sha256="a" * 64,
        status=PackageStatus.STAGED,
        integrity=PackageIntegrity.VERIFIED,
        compatible=True,
        available=False,
        scenarioCount=1,
        importedAt=now,
        enabledAt=None,
        disabledAt=None,
        pendingRemovalAt=None,
        removedAt=None,
    )

    class FakePackages:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def import_archive(self, archive: Path) -> PackageOperation:
            self.calls.append(f"import:{archive.name}")
            return PackageOperation(action="import", package=info)

        def list_packages(self, status: PackageStatus | None = None) -> tuple[PackageInfo, ...]:
            self.calls.append(f"list:{status.value if status else 'all'}")
            return (info,)

        def show(self, lab_id: str, version: str | None = None) -> tuple[PackageInfo, ...]:
            self.calls.append(f"show:{lab_id}:{version}")
            return (info,)

        def enable(self, lab_id: str, version: str) -> PackageOperation:
            self.calls.append(f"enable:{lab_id}:{version}")
            return PackageOperation(action="enable", package=info)

        def disable(self, lab_id: str) -> PackageOperation:
            self.calls.append(f"disable:{lab_id}")
            return PackageOperation(action="disable", package=info)

        def remove(self, lab_id: str, version: str) -> PackageOperation:
            self.calls.append(f"remove:{lab_id}:{version}")
            return PackageOperation(action="remove", package=info)

    packages = FakePackages()

    class Runtime:
        manager = object()
        kubeconfig_path = tmp_path / "kubeconfig"

        def __init__(self) -> None:
            self.packages = packages

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "build_package_runtime", Runtime)

    commands = (
        ["package", "import", "sample.kubelab-lab.tar.gz", "--json"],
        ["package", "list", "--status", "staged", "--json"],
        ["package", "show", "lab-local-networking", "--version", "1.0.0", "--json"],
        ["package", "enable", "lab-local-networking", "--version", "1.0.0", "--json"],
        ["package", "disable", "lab-local-networking", "--json"],
        [
            "package",
            "remove",
            "lab-local-networking",
            "--version",
            "1.0.0",
            "--yes",
            "--json",
        ],
    )
    for command in commands:
        result = RUNNER.invoke(app, command)
        assert result.exit_code == 0, result.stdout
        assert "lab-local-networking" in result.stdout

    before = tuple(packages.calls)
    rejected = RUNNER.invoke(
        app,
        [
            "package",
            "remove",
            "lab-local-networking",
            "--version",
            "1.0.0",
            "--json",
        ],
    )
    assert rejected.exit_code == 2
    assert json.loads(rejected.stderr)["code"] == "PACKAGE_CONFIRMATION_REQUIRED"
    assert tuple(packages.calls) == before


def test_missing_targets_idempotent_enable_and_removed_version_are_stable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        with pytest.raises(PackageManagerError) as missing_show:
            manager.show("lab-missing")
        assert missing_show.value.code == "PACKAGE_NOT_FOUND"
        with pytest.raises(PackageManagerError) as missing_enable:
            manager.enable("lab-missing", "1.0.0")
        assert missing_enable.value.code == "PACKAGE_NOT_AVAILABLE"
        with pytest.raises(PackageManagerError) as missing_disable:
            manager.disable("lab-missing")
        assert missing_disable.value.code == "PACKAGE_NOT_ENABLED"
        with pytest.raises(PackageManagerError) as missing_remove:
            manager.remove("lab-missing", "1.0.0")
        assert missing_remove.value.code == "PACKAGE_NOT_FOUND"

        archive = _archive(tmp_path)
        manager.import_archive(archive)
        manager.enable("lab-local-networking", "1.0.0")
        repeated_enable = manager.enable("lab-local-networking", "1.0.0")
        assert repeated_enable.idempotent
        manager.remove("lab-local-networking", "1.0.0")
        repeated_remove = manager.remove("lab-local-networking", "1.0.0")
        assert repeated_remove.idempotent
        assert repeated_remove.package.status is PackageStatus.REMOVED

        reimported = manager.import_archive(archive)
        assert not reimported.idempotent
        assert reimported.package.status is PackageStatus.STAGED
        assert manager.list_packages(PackageStatus.STAGED) == (reimported.package,)
    finally:
        database.dispose()


def test_storage_boundaries_and_inventory_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        archive = _archive(tmp_path)
        unsafe_root = tmp_path / "unsafe-packages"
        unsafe_root.write_text("not a directory", encoding="utf-8")
        unsafe = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=unsafe_root,
            builtin_registry=LabRegistry(LABS_ROOT),
            platform_supported=lambda: True,
        )
        with pytest.raises(PackageManagerError) as unsafe_storage:
            unsafe.import_archive(archive)
        assert unsafe_storage.value.code == "PACKAGE_STORAGE_UNSAFE"

        manager = _manager(tmp_path, database)
        with pytest.raises(PackageManagerError) as bad_digest:
            manager._blob_path("not-a-digest")
        assert bad_digest.value.code == "PACKAGE_DIGEST_INVALID"
        with pytest.raises(PackageManagerError) as outside:
            manager._remove_blob(tmp_path / ("a" * 64))
        assert outside.value.code == "PACKAGE_STORAGE_BOUNDARY"

        monkeypatch.setattr(package_manager_module, "MAX_REGISTERED_VERSIONS", 0)
        with pytest.raises(PackageManagerError) as inventory:
            manager.import_archive(archive)
        assert inventory.value.code == "PACKAGE_INVENTORY_LIMIT"
        monkeypatch.setattr(package_manager_module, "MAX_REGISTERED_VERSIONS", 128)
        monkeypatch.setattr(package_manager_module, "MAX_PACKAGE_STORAGE_BYTES", 0)
        with pytest.raises(PackageManagerError) as storage:
            manager.import_archive(archive)
        assert storage.value.code == "PACKAGE_STORAGE_LIMIT"
    finally:
        database.dispose()


def test_registered_version_limit_includes_removed_inventory_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        manager.import_archive(_archive(tmp_path, version="1.0.0"))
        manager.remove("lab-local-networking", "1.0.0")
        monkeypatch.setattr(package_manager_module, "MAX_REGISTERED_VERSIONS", 1)

        with pytest.raises(PackageManagerError) as caught:
            manager.import_archive(_archive(tmp_path, version="1.1.0"))

        assert caught.value.code == "PACKAGE_INVENTORY_LIMIT"
    finally:
        database.dispose()


def test_import_detects_preexisting_digest_directory_and_internal_errors_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        archive = _archive(tmp_path)
        manager = _manager(tmp_path, database)
        verified = package_manager_module.verify_lab_archive(archive)
        occupied = tmp_path / "packages" / "blobs" / verified.sha256
        occupied.mkdir(parents=True)
        with pytest.raises(PackageManagerError) as conflict:
            manager.import_archive(archive)
        assert conflict.value.code == "PACKAGE_STORAGE_CONFLICT"

        occupied.rmdir()
        monkeypatch.setattr(
            PackageManager,
            "_validate_extracted",
            staticmethod(lambda content, value: (_ for _ in ()).throw(RuntimeError("Bearer x"))),
        )
        with pytest.raises(PackageManagerError) as internal:
            manager.import_archive(archive)
        assert internal.value.code == "PACKAGE_INTERNAL_ERROR"
        assert internal.value.exit_code == 10
        assert "Bearer" not in internal.value.message
    finally:
        database.dispose()


def test_offline_verifier_wraps_unexpected_failures_without_leaking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive(tmp_path)
    monkeypatch.setattr(
        package_manager_module,
        "verify_lab_archive",
        lambda value: (_ for _ in ()).throw(RuntimeError("Traceback Bearer private-token")),
    )

    with pytest.raises(PackageManagerError) as caught:
        verify_package_archive(archive)

    assert caught.value.code == "PACKAGE_INTERNAL_ERROR"
    assert caught.value.exit_code == 10
    assert "private-token" not in caught.value.message


def test_removal_failure_stays_pending_and_is_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        manager = _manager(tmp_path, database)
        manager.import_archive(_archive(tmp_path))

        original = manager._remove_blob
        monkeypatch.setattr(
            manager,
            "_remove_blob",
            lambda path: (_ for _ in ()).throw(OSError("locked")),
        )
        with pytest.raises(PackageManagerError) as deferred:
            manager.remove("lab-local-networking", "1.0.0")
        assert deferred.value.code == "PACKAGE_REMOVAL_DEFERRED"
        with database.unit_of_work() as uow:
            assert uow.packages.list_all()[0].status is PackageStatus.PENDING_REMOVAL
        assert manager.list_packages()[0].status is PackageStatus.PENDING_REMOVAL

        monkeypatch.setattr(manager, "_remove_blob", original)
        manager.sweep_pending_removals()
        assert manager.list_packages()[0].status is PackageStatus.REMOVED
    finally:
        database.dispose()


def test_package_cli_human_output_cancellation_and_service_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive(tmp_path)
    verified = RUNNER.invoke(app, ["package", "verify", str(archive)])
    assert verified.exit_code == 0
    assert "Package verified" in verified.stdout
    assert "importable: yes" in verified.stdout

    now = datetime(2026, 9, 2, tzinfo=UTC)
    info = PackageInfo(
        labId="lab-local-networking",
        packageVersion="1.0.0",
        publisherId="local-author",
        publisherName="Local Author",
        publisherVerified=False,
        requiresKubelab=">=0.6.0a0,<0.7.0",
        formatVersion=2,
        archiveSize=1024,
        sha256="a" * 64,
        status=PackageStatus.STAGED,
        integrity=PackageIntegrity.VERIFIED,
        compatible=True,
        available=False,
        scenarioCount=1,
        importedAt=now,
        enabledAt=None,
        disabledAt=None,
        pendingRemovalAt=None,
        removedAt=None,
    )

    class FakePackages:
        def list_packages(self, status: PackageStatus | None = None) -> tuple[PackageInfo, ...]:
            del status
            return (info,)

        def enable(self, lab_id: str, version: str) -> PackageOperation:
            del lab_id, version
            return PackageOperation(action="enable", package=info, idempotent=True)

        def remove(self, lab_id: str, version: str) -> PackageOperation:
            del lab_id, version
            return PackageOperation(action="remove", package=info, deferred=True)

    class Runtime:
        manager = object()
        kubeconfig_path = tmp_path / "kubeconfig"
        packages: object | None = FakePackages()

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "build_package_runtime", Runtime)
    listed = RUNNER.invoke(app, ["package", "list"])
    assert listed.exit_code == 0
    assert "publisher-unverified" in listed.stdout
    enabled = RUNNER.invoke(
        app,
        ["package", "enable", "lab-local-networking", "--version", "1.0.0"],
    )
    assert enabled.exit_code == 0
    assert "No change was required" in enabled.stdout
    removed = RUNNER.invoke(
        app,
        [
            "package",
            "remove",
            "lab-local-networking",
            "--version",
            "1.0.0",
            "--yes",
        ],
    )
    assert removed.exit_code == 0
    assert "removal is deferred" in removed.stdout

    Runtime.packages = None
    unavailable = RUNNER.invoke(app, ["package", "list", "--json"])
    assert unavailable.exit_code == 5
    assert json.loads(unavailable.stderr)["code"] == "PACKAGE_SERVICE_UNAVAILABLE"

    invalid = RUNNER.invoke(app, ["package", "verify", str(tmp_path / "missing"), "--json"])
    assert invalid.exit_code == 2
    assert json.loads(invalid.stderr)["code"] == "PACKAGE_ARCHIVE_NOT_FOUND"
    assert str(tmp_path) not in invalid.stderr


def test_incompatible_v2_and_invalid_import_archive_use_stable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        archive = _archive(tmp_path)
        manager = _manager(tmp_path, database)
        original = package_manager_module.verify_lab_archive(archive)
        incompatible = original.model_copy(
            update={
                "importable": False,
                "compatible": False,
                "compatibility_message": "A different KubeLab version is required.",
            }
        )
        monkeypatch.setattr(
            package_manager_module,
            "verify_lab_archive",
            lambda value: incompatible,
        )
        with pytest.raises(PackageManagerError) as version:
            manager.import_archive(archive)
        assert version.value.code == "PACKAGE_VERSION_INCOMPATIBLE"

        monkeypatch.undo()
        with pytest.raises(PackageManagerError) as invalid:
            manager.import_archive(tmp_path / "missing.kubelab-lab.tar.gz")
        assert invalid.value.code == "PACKAGE_ARCHIVE_NOT_FOUND"
    finally:
        database.dispose()


def test_fake_lifecycle_failures_keep_lint_and_contract_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = package_manager_module.verify_lab_archive(_archive(tmp_path))

    class UnsafeAuthoring:
        def __init__(self, workspace: Path) -> None:
            del workspace

        def lint(self, family: Path) -> SimpleNamespace:
            del family
            issue = SimpleNamespace(
                severity=package_manager_module.IssueSeverity.ERROR,
                exit_code=3,
            )
            return SimpleNamespace(passed=False, issues=(issue,))

    monkeypatch.setattr(package_manager_module, "AuthoringService", UnsafeAuthoring)
    with pytest.raises(PackageManagerError) as unsafe:
        PackageManager._validate_extracted(tmp_path, verified)
    assert unsafe.value.code == "PACKAGE_AUTHORING_UNSAFE"
    assert unsafe.value.exit_code == 3

    class FailingContractAuthoring:
        def __init__(self, workspace: Path) -> None:
            del workspace

        def lint(self, family: Path) -> SimpleNamespace:
            del family
            return SimpleNamespace(passed=True, issues=())

        def test(self, family: Path) -> SimpleNamespace:
            del family
            return SimpleNamespace(passed=False)

    monkeypatch.setattr(package_manager_module, "AuthoringService", FailingContractAuthoring)
    with pytest.raises(PackageManagerError) as failed:
        PackageManager._validate_extracted(tmp_path, verified)
    assert failed.value.code == "PACKAGE_FAKE_CONTRACT_FAILED"
    assert failed.value.exit_code == 4


def test_blob_storage_shape_copy_digest_and_platform_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    try:
        state_root = tmp_path / "state-root"
        state_root.mkdir()
        (state_root / "blobs").write_text("not a directory", encoding="utf-8")
        manager = PackageManager(
            unit_of_work=database.unit_of_work,
            state_root=state_root,
            builtin_registry=LabRegistry(LABS_ROOT),
            platform_supported=lambda: True,
        )
        with pytest.raises(PackageManagerError) as unsafe_size:
            manager._stored_bytes()
        assert unsafe_size.value.code == "PACKAGE_STORAGE_UNSAFE"
        with pytest.raises(PackageManagerError) as unsafe:
            manager._prepare_state_root()
        assert unsafe.value.code == "PACKAGE_STORAGE_UNSAFE"

        safe = _manager(tmp_path, database)
        missing = tmp_path / "packages" / "blobs" / ("b" * 64)
        safe._remove_blob(missing)
        missing.parent.mkdir(parents=True, exist_ok=True)
        missing.write_text("not a directory", encoding="utf-8")
        with pytest.raises(PackageManagerError) as unsafe_blob:
            safe._remove_blob(missing)
        assert unsafe_blob.value.code == "PACKAGE_STORAGE_UNSAFE"

        source = tmp_path / "copy-source"
        destination = tmp_path / "copy-destination"
        source.write_bytes(b"content")
        with pytest.raises(package_manager_module.PackageArchiveError) as changed:
            PackageManager._copy_archive(source, destination, expected_sha256="0" * 64)
        assert changed.value.code == "PACKAGE_ARCHIVE_CHANGED"
        assert not destination.exists()

        monkeypatch.setattr(package_manager_module.platform, "system", lambda: "Linux")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        assert package_manager_module.is_package_platform_supported()
        monkeypatch.setenv("WSL_DISTRO_NAME", "Debian")
        assert not package_manager_module.is_package_platform_supported()
    finally:
        database.dispose()
