"""Runtime composition of built-in labs and enabled local package registries."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from kubelab.lab_registry import (
    EffectiveLab,
    LabMaterializationError,
    LabRegistry,
    LoadedLab,
    RegistryError,
    RegistryErrorCode,
    RegistrySnapshot,
    _MaterializedLab,
)
from kubelab.package_archive import (
    PackageArchiveError,
    verify_extracted_content,
    verify_lab_archive,
)
from kubelab.package_state import LabPackageSnapshot, LabSource, PackageStatus
from kubelab.repositories import SqlAlchemyUnitOfWork


class PackageCatalogRegistry(LabRegistry):
    """Expose enabled packages to new Sessions and digest-pinned packages to active ones."""

    def __init__(
        self,
        *,
        builtin_registry: LabRegistry,
        unit_of_work: Callable[[], SqlAlchemyUnitOfWork],
        state_root: Path,
    ) -> None:
        super().__init__()
        self._builtin_registry = builtin_registry
        self._unit_of_work = unit_of_work
        self._state_root = state_root

    def scan(self) -> RegistrySnapshot:
        builtin = self._builtin_registry.scan()
        labs = list(builtin.labs)
        errors = list(builtin.errors)
        builtin_ids = {item.definition.metadata.id for item in builtin.labs}
        with self._unit_of_work() as uow:
            enabled = tuple(
                item for item in uow.packages.list_all() if item.status is PackageStatus.ENABLED
            )
        for package in enabled:
            if package.lab_id in builtin_ids:
                errors.append(self._source_error(package.lab_id))
                continue
            try:
                registry = self._registry_for(package)
                snapshot = registry.scan()
                matches = tuple(
                    lab for lab in snapshot.labs if lab.definition.metadata.id == package.lab_id
                )
                if snapshot.errors or len(matches) != 1 or len(snapshot.labs) != 1:
                    errors.extend(snapshot.errors)
                    errors.append(self._source_error(package.lab_id))
                    continue
                labs.append(matches[0])
            except PackageArchiveError:
                errors.append(self._source_error(package.lab_id))
        labs.sort(key=lambda item: (item.lab_path.casefold(), item.lab_path))
        errors.sort(
            key=lambda item: (
                item.lab_path.casefold(),
                item.lab_path,
                item.field_path or "",
                item.code.value,
            )
        )
        return RegistrySnapshot(labs=tuple(labs), errors=tuple(errors))

    def pinned_lab(self, lab_id: str, package_sha256: str | None = None) -> LoadedLab | None:
        if package_sha256 is None:
            return self._builtin_registry.pinned_lab(lab_id)
        try:
            registry = self._registry_for_digest(package_sha256)
        except PackageArchiveError:
            return None
        snapshot = registry.scan()
        if snapshot.errors:
            return None
        return next(
            (lab for lab in snapshot.labs if lab.definition.metadata.id == lab_id),
            None,
        )

    def resolve_variant(self, loaded: LoadedLab, variant_id: str) -> LoadedLab | EffectiveLab:
        return self._registry_for_loaded(loaded).resolve_variant(loaded, variant_id)

    def materialize_for_gateway(self, loaded: LoadedLab | EffectiveLab) -> _MaterializedLab:
        parent = loaded.parent if isinstance(loaded, EffectiveLab) else loaded
        return self._registry_for_loaded(parent).materialize_for_gateway(loaded)

    def _registry_for_loaded(self, loaded: LoadedLab) -> LabRegistry:
        if loaded.source is LabSource.BUILTIN:
            return self._builtin_registry
        if loaded.package_sha256 is None:
            raise LabMaterializationError((self._source_error(loaded.definition.metadata.id),))
        try:
            return self._registry_for_digest(loaded.package_sha256)
        except PackageArchiveError as exc:
            raise LabMaterializationError(
                (self._source_error(loaded.definition.metadata.id),)
            ) from exc

    def _registry_for_digest(self, sha256: str) -> LabRegistry:
        with self._unit_of_work() as uow:
            package = uow.packages.get(sha256)
        if package is None or package.status is PackageStatus.REMOVED:
            raise PackageArchiveError(
                "PACKAGE_CONTENT_UNAVAILABLE",
                "The Session's pinned local package is unavailable.",
            )
        return self._registry_for(package)

    def _registry_for(self, package: LabPackageSnapshot) -> LabRegistry:
        blob = self._state_root / "blobs" / package.sha256
        archive = blob / "archive.kubelab-lab.tar.gz"
        verified = verify_lab_archive(archive)
        if (
            verified.sha256 != package.sha256
            or verified.lab_id != package.lab_id
            or verified.package_version != package.package_version
            or verified.publisher_id != package.publisher_id
        ):
            raise PackageArchiveError(
                "PACKAGE_STORED_METADATA_MISMATCH",
                "Stored package content does not match its registered identity.",
            )
        verify_extracted_content(blob / "content", verified)
        return LabRegistry(
            blob / "content" / "labs",
            source=LabSource.LOCAL_PACKAGE,
            package_sha256=package.sha256,
            package_version=package.package_version,
            publisher_id=package.publisher_id,
            publisher_name=package.publisher_name,
        )

    @staticmethod
    def _source_error(lab_id: str) -> RegistryError:
        return RegistryError(
            code=RegistryErrorCode.LAB_SOURCE_CHANGED,
            message="The enabled local package is unavailable or failed integrity validation.",
            lab_path=f"local-package/{lab_id}",
            retryable=True,
            lab_id=lab_id,
        )


__all__ = ["PackageCatalogRegistry"]
