"""Application service for the trusted local lab-package lifecycle."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kubelab.authoring import AuthoringService, IssueSeverity
from kubelab.config import get_data_dir
from kubelab.lab_registry import LabRegistry
from kubelab.operation_lock import OperationLock
from kubelab.package_archive import (
    PackageArchiveError,
    VerifiedLabArchive,
    extract_verified_archive,
    verify_extracted_content,
    verify_lab_archive,
)
from kubelab.package_schema import package_version_key
from kubelab.package_state import LabPackageSnapshot, NewLabPackage, PackageStatus
from kubelab.repositories import (
    PackageVersionConflict,
    SqlAlchemyUnitOfWork,
)

MAX_PACKAGE_STORAGE_BYTES = 256 * 1024 * 1024
MAX_REGISTERED_VERSIONS = 128


class PackageIntegrity(StrEnum):
    VERIFIED = "verified"
    INVALID = "invalid"


class PackageManagerError(RuntimeError):
    """Stable public error that never includes a local path or internal exception."""

    def __init__(self, code: str, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class PackageManagerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PackageVerification(PackageManagerModel):
    filename: str
    sha256: str
    integrity: PackageIntegrity = PackageIntegrity.VERIFIED
    format_version: int = Field(alias="formatVersion")
    importable: bool
    compatible: bool
    compatibility_message: str = Field(alias="compatibilityMessage")
    lab_id: str = Field(alias="labId")
    package_version: str | None = Field(alias="packageVersion", default=None)
    publisher_id: str | None = Field(alias="publisherId", default=None)
    publisher_name: str | None = Field(alias="publisherName", default=None)
    publisher_verified: bool = Field(alias="publisherVerified", default=False)
    scenario_count: int = Field(alias="scenarioCount", ge=0)


class PackageInfo(PackageManagerModel):
    lab_id: str = Field(alias="labId")
    package_version: str = Field(alias="packageVersion")
    publisher_id: str = Field(alias="publisherId")
    publisher_name: str = Field(alias="publisherName")
    publisher_verified: bool = Field(alias="publisherVerified", default=False)
    requires_kubelab: str = Field(alias="requiresKubelab")
    format_version: int = Field(alias="formatVersion")
    archive_size: int = Field(alias="archiveSize")
    sha256: str
    status: PackageStatus
    integrity: PackageIntegrity
    compatible: bool
    available: bool
    scenario_count: int = Field(alias="scenarioCount", ge=0)
    imported_at: datetime = Field(alias="importedAt")
    enabled_at: datetime | None = Field(alias="enabledAt")
    disabled_at: datetime | None = Field(alias="disabledAt")
    pending_removal_at: datetime | None = Field(alias="pendingRemovalAt")
    removed_at: datetime | None = Field(alias="removedAt")


class PackageOperation(PackageManagerModel):
    action: str
    package: PackageInfo
    idempotent: bool = False
    deferred: bool = False


class PackageManager:
    """Verify, stage, activate, roll back, and safely retire local packages."""

    def __init__(
        self,
        *,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
        state_root: Path | None = None,
        builtin_registry: LabRegistry | None = None,
        operation_lock: OperationLock | None = None,
        platform_supported: Callable[[], bool] | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._state_root = state_root or (get_data_dir() / "packages")
        self._builtin_registry = builtin_registry or LabRegistry()
        self._operation_lock = operation_lock
        self._platform_supported = platform_supported or is_package_platform_supported

    def verify(self, archive: Path) -> PackageVerification:
        return verify_package_archive(archive)

    def import_archive(self, archive: Path) -> PackageOperation:
        self._require_supported_platform()
        with self._lock():
            self._sweep_pending_removals()
            try:
                verified = verify_lab_archive(archive)
            except PackageArchiveError as exc:
                raise PackageManagerError(exc.code, exc.message, exit_code=exc.exit_code) from exc
            if not verified.importable or verified.format_version != 2:
                code = (
                    "PACKAGE_FORMAT_NOT_IMPORTABLE"
                    if verified.format_version == 1
                    else "PACKAGE_VERSION_INCOMPATIBLE"
                )
                raise PackageManagerError(
                    code,
                    verified.compatibility_message,
                    exit_code=2,
                )
            self._require_external_identity(verified)
            existing = self._preflight_inventory(verified)
            if existing is not None and existing.status is not PackageStatus.REMOVED:
                info = self._public_info(existing)
                if info.integrity is PackageIntegrity.INVALID:
                    raise PackageManagerError(
                        "PACKAGE_STORED_CONTENT_INVALID",
                        "The registered package content failed local integrity validation.",
                        exit_code=3,
                    )
                return PackageOperation(
                    action="import",
                    package=info,
                    idempotent=True,
                )
            self._prepare_state_root()
            blobs = self._state_root / "blobs"
            staging = blobs / f".import-{uuid4().hex}"
            final = blobs / verified.sha256
            final_created = False
            try:
                staging.mkdir()
                content = staging / "content"
                extract_verified_archive(archive, verified, content)
                self._validate_extracted(content, verified)
                self._copy_archive(
                    archive,
                    staging / "archive.kubelab-lab.tar.gz",
                    expected_sha256=verified.sha256,
                )
                if final.exists():
                    raise PackageManagerError(
                        "PACKAGE_STORAGE_CONFLICT",
                        "Unregistered content already occupies the package digest directory.",
                        exit_code=5,
                    )
                staging.replace(final)
                final_created = True
                with self._unit_of_work() as uow:
                    if existing is None:
                        snapshot = uow.packages.add(_new_package(verified))
                    else:
                        snapshot = uow.packages.set_status(
                            verified.sha256,
                            PackageStatus.STAGED,
                            event_type="reimported",
                        )
                    uow.commit()
                return PackageOperation(
                    action="import",
                    package=self._public_info(snapshot),
                    idempotent=False,
                )
            except PackageManagerError:
                if final_created:
                    self._remove_blob(final)
                raise
            except PackageArchiveError as exc:
                if final_created:
                    self._remove_blob(final)
                raise PackageManagerError(exc.code, exc.message, exit_code=exc.exit_code) from exc
            except (OSError, PackageVersionConflict) as exc:
                if final_created:
                    self._remove_blob(final)
                raise PackageManagerError(
                    "PACKAGE_IMPORT_FAILED",
                    "The package could not be committed to trusted local storage.",
                    exit_code=5,
                ) from exc
            except Exception as exc:
                if final_created:
                    self._remove_blob(final)
                raise PackageManagerError(
                    "PACKAGE_INTERNAL_ERROR",
                    "KubeLab could not safely complete the package import.",
                    exit_code=10,
                ) from exc
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def list_packages(self, status: PackageStatus | None = None) -> tuple[PackageInfo, ...]:
        self._require_supported_platform()
        with self._lock():
            with self._unit_of_work() as uow:
                records = uow.packages.list_all()
            if status is not None:
                records = tuple(item for item in records if item.status is status)
            return tuple(self._public_info(item) for item in records)

    def sweep_pending_removals(self) -> None:
        """Best-effort removal pass used after a Session reaches completed."""
        self._require_supported_platform()
        with self._lock():
            self._sweep_pending_removals()

    def show(self, lab_id: str, package_version: str | None = None) -> tuple[PackageInfo, ...]:
        packages = tuple(item for item in self.list_packages() if item.lab_id == lab_id)
        if package_version is not None:
            packages = tuple(item for item in packages if item.package_version == package_version)
        if not packages:
            raise PackageManagerError(
                "PACKAGE_NOT_FOUND",
                "No registered local package matches the requested experiment.",
                exit_code=2,
            )
        return tuple(sorted(packages, key=lambda item: package_version_key(item.package_version)))

    def enable(self, lab_id: str, package_version: str) -> PackageOperation:
        self._require_supported_platform()
        with self._lock():
            self._sweep_pending_removals()
            with self._unit_of_work() as uow:
                target = uow.packages.find_version(lab_id, package_version)
                if target is None or target.status in {
                    PackageStatus.PENDING_REMOVAL,
                    PackageStatus.REMOVED,
                }:
                    raise PackageManagerError(
                        "PACKAGE_NOT_AVAILABLE",
                        "The requested package version is not available for activation.",
                        exit_code=2,
                    )
            self._require_stored_archive(target)
            with self._unit_of_work() as uow:
                current = uow.packages.get_enabled(lab_id)
                if current is not None and current.sha256 == target.sha256:
                    return PackageOperation(
                        action="enable",
                        package=self._public_info(current),
                        idempotent=True,
                    )
                if current is not None:
                    uow.packages.set_status(
                        current.sha256,
                        PackageStatus.DISABLED,
                        event_type="version_switched",
                        context={"nextSha256": target.sha256},
                    )
                enabled = uow.packages.set_status(
                    target.sha256,
                    PackageStatus.ENABLED,
                    event_type="enabled" if current is None else "version_switched",
                    context={"previousSha256": current.sha256} if current else None,
                )
                uow.commit()
            return PackageOperation(action="enable", package=self._public_info(enabled))

    def disable(self, lab_id: str) -> PackageOperation:
        self._require_supported_platform()
        with self._lock():
            self._sweep_pending_removals()
            with self._unit_of_work() as uow:
                current = uow.packages.get_enabled(lab_id)
                if current is None:
                    raise PackageManagerError(
                        "PACKAGE_NOT_ENABLED",
                        "The experiment does not have an enabled local package.",
                        exit_code=2,
                    )
                disabled = uow.packages.set_status(
                    current.sha256,
                    PackageStatus.DISABLED,
                    event_type="disabled",
                )
                uow.commit()
            return PackageOperation(action="disable", package=self._public_info(disabled))

    def remove(self, lab_id: str, package_version: str) -> PackageOperation:
        self._require_supported_platform()
        with self._lock():
            self._sweep_pending_removals()
            with self._unit_of_work() as uow:
                target = uow.packages.find_version(lab_id, package_version)
                if target is None:
                    raise PackageManagerError(
                        "PACKAGE_NOT_FOUND",
                        "The requested package version is not registered.",
                        exit_code=2,
                    )
                if target.status is PackageStatus.REMOVED:
                    return PackageOperation(
                        action="remove",
                        package=self._public_info(target),
                        idempotent=True,
                    )
                active_count = uow.packages.active_session_count(target.sha256)
                pending = uow.packages.set_status(
                    target.sha256,
                    PackageStatus.PENDING_REMOVAL,
                    event_type="removal_requested",
                    context={"activeSession": active_count > 0},
                )
                uow.commit()
            if active_count:
                return PackageOperation(
                    action="remove",
                    package=self._public_info(pending),
                    deferred=True,
                )
            removed = self._finalize_removal(pending)
            return PackageOperation(action="remove", package=self._public_info(removed))

    def _preflight_inventory(self, verified: VerifiedLabArchive) -> LabPackageSnapshot | None:
        assert verified.package_version is not None
        assert verified.publisher_id is not None
        with self._unit_of_work() as uow:
            records = uow.packages.list_all()
            existing = uow.packages.get(verified.sha256)
        if len(records) >= MAX_REGISTERED_VERSIONS and existing is None:
            raise PackageManagerError(
                "PACKAGE_INVENTORY_LIMIT",
                "The local package inventory already contains 128 registered versions.",
                exit_code=5,
            )
        will_store = existing is None or existing.status is PackageStatus.REMOVED
        incoming_size = verified.archive_size + sum(item.size for item in verified.members)
        if will_store and self._stored_bytes() + incoming_size > MAX_PACKAGE_STORAGE_BYTES:
            raise PackageManagerError(
                "PACKAGE_STORAGE_LIMIT",
                "The local package inventory would exceed the 256 MiB storage limit.",
                exit_code=5,
            )
        same_lab = tuple(item for item in records if item.lab_id == verified.lab_id)
        if any(item.publisher_id != verified.publisher_id for item in same_lab):
            raise PackageManagerError(
                "PACKAGE_PUBLISHER_CONFLICT",
                "This experiment ID is already locked to another self-declared publisher.",
                exit_code=3,
            )
        same_version = next(
            (
                item
                for item in same_lab
                if item.publisher_id == verified.publisher_id
                and item.package_version == verified.package_version
            ),
            None,
        )
        if same_version is not None and same_version.sha256 != verified.sha256:
            raise PackageManagerError(
                "PACKAGE_VERSION_CONFLICT",
                "The same publisher, experiment ID, and version already have different content.",
                exit_code=3,
            )
        return existing

    def _stored_bytes(self) -> int:
        blobs = self._state_root / "blobs"
        if not blobs.exists():
            return 0
        try:
            metadata = blobs.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError
            total = 0
            for candidate in blobs.rglob("*"):
                candidate_metadata = candidate.lstat()
                if stat.S_ISLNK(candidate_metadata.st_mode):
                    raise OSError
                if stat.S_ISREG(candidate_metadata.st_mode):
                    total += candidate_metadata.st_size
                elif not stat.S_ISDIR(candidate_metadata.st_mode):
                    raise OSError
            return total
        except OSError as exc:
            raise PackageManagerError(
                "PACKAGE_STORAGE_UNSAFE",
                "The package storage contains an unsafe filesystem entry.",
                exit_code=5,
            ) from exc

    def _require_external_identity(self, verified: VerifiedLabArchive) -> None:
        builtins = {item.definition.metadata.id for item in self._builtin_registry.scan().labs}
        if verified.lab_id in builtins:
            raise PackageManagerError(
                "PACKAGE_BUILTIN_ID_RESERVED",
                "Local packages cannot replace a built-in KubeLab experiment ID.",
                exit_code=3,
            )

    def _validate_fake_lifecycle(self, archive: Path, verified: VerifiedLabArchive) -> None:
        with tempfile.TemporaryDirectory(prefix="kubelab-package-verify-") as temporary:
            root = Path(temporary)
            content = root / "content"
            extract_verified_archive(archive, verified, content)
            self._validate_extracted(content, verified)

    @staticmethod
    def _validate_extracted(content: Path, verified: VerifiedLabArchive) -> None:
        family = content.joinpath(*Path(verified.family_directory).parts)
        service = AuthoringService(content)
        lint = service.lint(family)
        if not lint.passed:
            security = any(
                issue.severity is IssueSeverity.ERROR and issue.exit_code == 3
                for issue in lint.issues
            )
            raise PackageManagerError(
                "PACKAGE_AUTHORING_UNSAFE" if security else "PACKAGE_AUTHORING_INVALID",
                "The package failed the declarative authoring and safety contract.",
                exit_code=3 if security else 2,
            )
        tested = service.test(family)
        if not tested.passed:
            raise PackageManagerError(
                "PACKAGE_FAKE_CONTRACT_FAILED",
                "The package failed its declared Fake lifecycle contract.",
                exit_code=4,
            )

    def _require_stored_archive(self, package: LabPackageSnapshot) -> VerifiedLabArchive:
        archive = self._blob_path(package.sha256) / "archive.kubelab-lab.tar.gz"
        try:
            verified = verify_lab_archive(archive)
            if verified.sha256 != package.sha256:
                raise PackageArchiveError(
                    "PACKAGE_STORED_DIGEST_MISMATCH",
                    "Stored package content does not match the registered digest.",
                )
            verify_extracted_content(self._blob_path(package.sha256) / "content", verified)
            self._validate_fake_lifecycle(archive, verified)
            return verified
        except (PackageArchiveError, PackageManagerError) as exc:
            if isinstance(exc, PackageManagerError):
                raise
            raise PackageManagerError(exc.code, exc.message, exit_code=exc.exit_code) from exc

    def _public_info(self, package: LabPackageSnapshot) -> PackageInfo:
        integrity = PackageIntegrity.INVALID
        compatible = False
        scenario_count = 0
        if package.status is not PackageStatus.REMOVED:
            try:
                verified = verify_lab_archive(
                    self._blob_path(package.sha256) / "archive.kubelab-lab.tar.gz"
                )
                if verified.sha256 == package.sha256:
                    verify_extracted_content(self._blob_path(package.sha256) / "content", verified)
                    integrity = PackageIntegrity.VERIFIED
                    compatible = verified.compatible
                    scenario_count = len(verified.scenarios)
            except PackageArchiveError:
                pass
        return PackageInfo(
            labId=package.lab_id,
            packageVersion=package.package_version,
            publisherId=package.publisher_id,
            publisherName=package.publisher_name,
            publisherVerified=False,
            requiresKubelab=package.requires_kubelab,
            formatVersion=package.format_version,
            archiveSize=package.archive_size,
            sha256=package.sha256,
            status=package.status,
            integrity=integrity,
            compatible=compatible,
            available=(
                package.status is PackageStatus.ENABLED
                and integrity is PackageIntegrity.VERIFIED
                and compatible
            ),
            scenarioCount=scenario_count,
            importedAt=package.imported_at,
            enabledAt=package.enabled_at,
            disabledAt=package.disabled_at,
            pendingRemovalAt=package.pending_removal_at,
            removedAt=package.removed_at,
        )

    def _sweep_pending_removals(self) -> None:
        with self._unit_of_work() as uow:
            pending = tuple(
                item
                for item in uow.packages.list_all()
                if item.status is PackageStatus.PENDING_REMOVAL
                and uow.packages.active_session_count(item.sha256) == 0
            )
        for item in pending:
            try:
                self._finalize_removal(item)
            except PackageManagerError:
                continue

    def _finalize_removal(self, package: LabPackageSnapshot) -> LabPackageSnapshot:
        try:
            self._remove_blob(self._blob_path(package.sha256))
        except OSError as exc:
            raise PackageManagerError(
                "PACKAGE_REMOVAL_DEFERRED",
                "Package files could not be removed; removal remains pending.",
                exit_code=5,
            ) from exc
        with self._unit_of_work() as uow:
            removed = uow.packages.set_status(
                package.sha256,
                PackageStatus.REMOVED,
                event_type="removed",
            )
            uow.commit()
        return removed

    def _prepare_state_root(self) -> None:
        if self._state_root.exists() and (
            self._state_root.is_symlink() or not self._state_root.is_dir()
        ):
            raise PackageManagerError(
                "PACKAGE_STORAGE_UNSAFE",
                "The package state location is not a safe directory.",
                exit_code=5,
            )
        self._state_root.mkdir(parents=True, exist_ok=True)
        blobs = self._state_root / "blobs"
        if blobs.exists() and (blobs.is_symlink() or not blobs.is_dir()):
            raise PackageManagerError(
                "PACKAGE_STORAGE_UNSAFE",
                "The package blob location is not a safe directory.",
                exit_code=5,
            )
        blobs.mkdir(exist_ok=True)

    def _blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise PackageManagerError(
                "PACKAGE_DIGEST_INVALID",
                "The registered package digest is invalid.",
                exit_code=10,
            )
        return self._state_root / "blobs" / sha256

    def _remove_blob(self, path: Path) -> None:
        expected_parent = (self._state_root / "blobs").resolve()
        if path.parent.resolve() != expected_parent or len(path.name) != 64:
            raise PackageManagerError(
                "PACKAGE_STORAGE_BOUNDARY",
                "The package removal target is outside trusted storage.",
                exit_code=10,
            )
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise PackageManagerError(
                "PACKAGE_STORAGE_UNSAFE",
                "The package removal target is not an owned digest directory.",
                exit_code=5,
            )
        shutil.rmtree(path)

    @staticmethod
    def _copy_archive(source: Path, destination: Path, *, expected_sha256: str) -> None:
        digest = hashlib.sha256()
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise PackageArchiveError(
                "PACKAGE_ARCHIVE_CHANGED",
                "The package archive changed while it was being copied.",
            )

    def _require_supported_platform(self) -> None:
        if not self._platform_supported():
            raise PackageManagerError(
                "PACKAGE_PLATFORM_UNSUPPORTED",
                "Package inventory changes are supported only inside WSL2 Ubuntu.",
                exit_code=5,
            )

    def _lock(self) -> OperationLock | nullcontext[None]:
        if self._operation_lock is not None:
            return self._operation_lock
        return nullcontext()


def _new_package(verified: VerifiedLabArchive) -> NewLabPackage:
    assert verified.package_version is not None
    assert verified.publisher_id is not None
    assert verified.publisher_name is not None
    assert verified.requires_kubelab is not None
    return NewLabPackage(
        sha256=verified.sha256,
        lab_id=verified.lab_id,
        package_version=verified.package_version,
        publisher_id=verified.publisher_id,
        publisher_name=verified.publisher_name,
        requires_kubelab=verified.requires_kubelab,
        format_version=verified.format_version,
        archive_size=verified.archive_size,
        imported_at=datetime.now(UTC),
    )


def _verification(verified: VerifiedLabArchive) -> PackageVerification:
    return PackageVerification(
        filename=verified.filename,
        sha256=verified.sha256,
        formatVersion=verified.format_version,
        importable=verified.importable,
        compatible=verified.compatible,
        compatibilityMessage=verified.compatibility_message,
        labId=verified.lab_id,
        packageVersion=verified.package_version,
        publisherId=verified.publisher_id,
        publisherName=verified.publisher_name,
        publisherVerified=False,
        scenarioCount=len(verified.scenarios),
    )


def verify_package_archive(archive: Path) -> PackageVerification:
    """Run cross-platform full verification without constructing learner state."""
    try:
        verified = verify_lab_archive(archive)
        if verified.format_version == 2:
            with tempfile.TemporaryDirectory(prefix="kubelab-package-verify-") as temporary:
                root = Path(temporary)
                content = root / "content"
                extract_verified_archive(archive, verified, content)
                PackageManager._validate_extracted(content, verified)
        return _verification(verified)
    except PackageArchiveError as exc:
        raise PackageManagerError(exc.code, exc.message, exit_code=exc.exit_code) from exc
    except PackageManagerError:
        raise
    except Exception as exc:
        raise PackageManagerError(
            "PACKAGE_INTERNAL_ERROR",
            "KubeLab could not safely complete package verification.",
            exit_code=10,
        ) from exc


def is_package_platform_supported() -> bool:
    distribution = os.environ.get("WSL_DISTRO_NAME", "").casefold()
    return platform.system() == "Linux" and distribution.startswith("ubuntu")


__all__ = [
    "MAX_PACKAGE_STORAGE_BYTES",
    "MAX_REGISTERED_VERSIONS",
    "PackageInfo",
    "PackageIntegrity",
    "PackageManager",
    "PackageManagerError",
    "PackageOperation",
    "PackageVerification",
    "is_package_platform_supported",
    "verify_package_archive",
]
