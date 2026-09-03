"""Shared, offline verification and extraction for KubeLab lab archives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kubelab import __version__
from kubelab.lab_schema import LabDefinition
from kubelab.package_schema import LabPackageDefinition, LabPackageIndex
from kubelab.safe_yaml import load_all_unique

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_INDEXED_CONTENT_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 257
MAX_MEMBER_BYTES = 512 * 1024


class PackageArchiveError(ValueError):
    """Stable, sanitized archive rejection with CLI-compatible severity."""

    def __init__(self, code: str, message: str, *, exit_code: int = 3) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class ArchiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class VerifiedArchiveMember(ArchiveModel):
    path: str
    size: int = Field(ge=0, le=MAX_MEMBER_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerifiedLabArchive(ArchiveModel):
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(alias="archiveSize", ge=0, le=MAX_ARCHIVE_BYTES)
    format_version: int = Field(alias="formatVersion", ge=1, le=2)
    importable: bool
    compatible: bool
    compatibility_message: str = Field(alias="compatibilityMessage")
    lab_id: str = Field(alias="labId")
    package_version: str | None = Field(alias="packageVersion", default=None)
    publisher_id: str | None = Field(alias="publisherId", default=None)
    publisher_name: str | None = Field(alias="publisherName", default=None)
    requires_kubelab: str | None = Field(alias="requiresKubelab", default=None)
    scenarios: tuple[str, ...]
    family_directory: str = Field(alias="familyDirectory")
    members: tuple[VerifiedArchiveMember, ...]


def verify_lab_archive(
    path: Path,
    *,
    kubelab_version: str = __version__,
) -> VerifiedLabArchive:
    """Verify one local archive without writing state or extracting files."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PackageArchiveError(
            "PACKAGE_ARCHIVE_NOT_FOUND",
            "The package archive does not exist or cannot be read.",
            exit_code=2,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PackageArchiveError(
            "PACKAGE_ARCHIVE_NOT_REGULAR",
            "The package archive must be a regular file and cannot be a symbolic link.",
        )
    if metadata.st_size > MAX_ARCHIVE_BYTES:
        raise PackageArchiveError(
            "PACKAGE_ARCHIVE_TOO_LARGE",
            "The package archive exceeds the 5 MiB compressed-size limit.",
        )
    digest = _file_sha256(path)
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_members(members)
            index_member = next((item for item in members if item.name == "index.json"), None)
            if index_member is None:
                raise PackageArchiveError(
                    "PACKAGE_INDEX_MISSING",
                    "The package archive does not contain index.json.",
                    exit_code=2,
                )
            if index_member.size > MAX_INDEX_BYTES:
                raise PackageArchiveError(
                    "PACKAGE_INDEX_TOO_LARGE",
                    "The package index exceeds its bounded size.",
                )
            index_content = _read_member(archive, index_member)
            index = _json_object(index_content)
            format_version = index.get("formatVersion")
            if format_version == 1:
                return _verify_v1(
                    path,
                    archive,
                    members,
                    index,
                    digest=digest,
                    archive_size=metadata.st_size,
                )
            if format_version != 2:
                raise PackageArchiveError(
                    "PACKAGE_FORMAT_UNSUPPORTED",
                    "Only KubeLab package format versions 1 and 2 can be verified.",
                    exit_code=2,
                )
            return _verify_v2(
                path,
                archive,
                members,
                index,
                digest=digest,
                archive_size=metadata.st_size,
                kubelab_version=kubelab_version,
            )
    except PackageArchiveError:
        raise
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageArchiveError(
            "PACKAGE_ARCHIVE_INVALID",
            "The package archive is not a readable gzip-compressed tar archive.",
            exit_code=2,
        ) from exc


def extract_verified_archive(
    archive_path: Path,
    verified: VerifiedLabArchive,
    destination: Path,
) -> None:
    """Extract verified regular members into a new, caller-owned directory."""
    if _file_sha256(archive_path) != verified.sha256:
        raise PackageArchiveError(
            "PACKAGE_ARCHIVE_CHANGED",
            "The package archive changed after verification.",
        )
    if destination.exists():
        raise PackageArchiveError(
            "PACKAGE_EXTRACTION_TARGET_EXISTS",
            "The package extraction target must not already exist.",
            exit_code=5,
        )
    destination.mkdir(parents=False)
    expected = {member.path: member for member in verified.members}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_members(members)
            actual = {member.name: member for member in members if member.name != "index.json"}
            if set(actual) != set(expected):
                raise PackageArchiveError(
                    "PACKAGE_CONTENT_CHANGED",
                    "The package content no longer matches its verified index.",
                )
            for name in sorted(actual):
                content = _read_member(archive, actual[name])
                indexed = expected[name]
                if len(content) != indexed.size or hashlib.sha256(content).hexdigest() != (
                    indexed.sha256
                ):
                    raise PackageArchiveError(
                        "PACKAGE_DIGEST_MISMATCH",
                        "A package file does not match its indexed digest.",
                    )
                output = destination.joinpath(*PurePosixPath(name).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(content)
    except Exception:
        if destination.exists():
            _remove_owned_tree(destination)
        raise


def verify_extracted_content(root: Path, verified: VerifiedLabArchive) -> None:
    """Verify that an extracted package tree still matches its immutable archive index."""
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise PackageArchiveError(
            "PACKAGE_CONTENT_UNAVAILABLE",
            "Stored package content is unavailable.",
            exit_code=5,
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PackageArchiveError(
            "PACKAGE_CONTENT_UNSAFE",
            "Stored package content is not a safe directory.",
        )

    expected = {member.path: member for member in verified.members}
    actual: dict[str, tuple[int, str]] = {}
    try:
        for candidate in root.rglob("*"):
            candidate_metadata = candidate.lstat()
            if stat.S_ISLNK(candidate_metadata.st_mode):
                raise PackageArchiveError(
                    "PACKAGE_CONTENT_UNSAFE",
                    "Stored package content contains an unsafe filesystem entry.",
                )
            if stat.S_ISDIR(candidate_metadata.st_mode):
                continue
            if not stat.S_ISREG(candidate_metadata.st_mode):
                raise PackageArchiveError(
                    "PACKAGE_CONTENT_UNSAFE",
                    "Stored package content contains an unsafe filesystem entry.",
                )
            actual[candidate.relative_to(root).as_posix()] = (
                candidate_metadata.st_size,
                _file_sha256(candidate),
            )
    except PackageArchiveError:
        raise
    except OSError as exc:
        raise PackageArchiveError(
            "PACKAGE_CONTENT_UNAVAILABLE",
            "Stored package content is unavailable.",
            exit_code=5,
        ) from exc

    if set(actual) != set(expected) or any(
        actual[path] != (member.size, member.sha256) for path, member in expected.items()
    ):
        raise PackageArchiveError(
            "PACKAGE_CONTENT_CHANGED",
            "Stored package content no longer matches its verified index.",
        )


def _verify_v2(
    path: Path,
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    raw_index: Mapping[str, Any],
    *,
    digest: str,
    archive_size: int,
    kubelab_version: str,
) -> VerifiedLabArchive:
    try:
        index = LabPackageIndex.model_validate(raw_index)
    except ValidationError as exc:
        raise PackageArchiveError(
            "PACKAGE_INDEX_INVALID",
            "The format v2 package index does not match the required schema.",
            exit_code=2,
        ) from exc
    indexed = _indexed_members(index.files)
    actual = {item.name: item for item in members if item.name != "index.json"}
    _verify_indexed_content(archive, indexed, actual)
    roots = _family_roots(tuple(actual))
    if len(roots) != 1:
        raise PackageArchiveError(
            "PACKAGE_FAMILY_INVALID",
            "A package must contain exactly one experiment family.",
            exit_code=2,
        )
    family = next(iter(roots))
    package_path = f"{family}/package.yaml"
    lab_path = f"{family}/lab.yaml"
    if package_path not in actual or lab_path not in actual:
        raise PackageArchiveError(
            "PACKAGE_CONTRACT_MISSING",
            "A format v2 package requires family-root lab.yaml and package.yaml files.",
            exit_code=2,
        )
    package = _load_package_yaml(_read_member(archive, actual[package_path]))
    lab = _load_lab_yaml(_read_member(archive, actual[lab_path]))
    projected = index.package
    if (
        index.lab_id != lab.metadata.id
        or package.metadata.lab_id != lab.metadata.id
        or package.metadata.version != projected.version
        or package.metadata.publisher_id != projected.publisher_id
        or package.metadata.publisher_name != projected.publisher_name
        or package.spec.requires_kubelab != projected.requires_kubelab
    ):
        raise PackageArchiveError(
            "PACKAGE_METADATA_MISMATCH",
            "Package metadata, index metadata, and lab identity must match.",
        )
    compatible = Version(kubelab_version) in SpecifierSet(package.spec.requires_kubelab)
    message = (
        "The package is compatible with this KubeLab version."
        if compatible
        else "The package requires a different KubeLab version."
    )
    return VerifiedLabArchive(
        filename=path.name,
        sha256=digest,
        archiveSize=archive_size,
        formatVersion=2,
        importable=compatible,
        compatible=compatible,
        compatibilityMessage=message,
        labId=lab.metadata.id,
        packageVersion=package.metadata.version,
        publisherId=package.metadata.publisher_id,
        publisherName=package.metadata.publisher_name,
        requiresKubelab=package.spec.requires_kubelab,
        scenarios=index.scenarios,
        familyDirectory=family,
        members=tuple(indexed.values()),
    )


def _verify_v1(
    path: Path,
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    raw_index: Mapping[str, Any],
    *,
    digest: str,
    archive_size: int,
) -> VerifiedLabArchive:
    lab_id = raw_index.get("labId")
    scenarios = raw_index.get("scenarios")
    files = raw_index.get("files")
    if (
        not isinstance(lab_id, str)
        or not lab_id
        or not isinstance(scenarios, list)
        or not all(isinstance(item, str) and item for item in scenarios)
        or not isinstance(files, list)
    ):
        raise PackageArchiveError(
            "PACKAGE_INDEX_INVALID",
            "The format v1 package index is incomplete.",
            exit_code=2,
        )
    indexed = _legacy_indexed_members(files)
    actual = {item.name: item for item in members if item.name != "index.json"}
    _verify_indexed_content(archive, indexed, actual)
    roots = _family_roots(tuple(actual))
    family = next(iter(roots)) if len(roots) == 1 else "labs/legacy"
    return VerifiedLabArchive(
        filename=path.name,
        sha256=digest,
        archiveSize=archive_size,
        formatVersion=1,
        importable=False,
        compatible=False,
        compatibilityMessage="Rebuild this legacy format v1 archive with KubeLab 0.6.",
        labId=lab_id,
        scenarios=tuple(scenarios),
        familyDirectory=family,
        members=tuple(indexed.values()),
    )


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise PackageArchiveError(
            "PACKAGE_MEMBER_LIMIT",
            "The package archive has an invalid number of members.",
        )
    exact: set[str] = set()
    folded: set[str] = set()
    for member in members:
        if not member.isfile() or member.issym() or member.islnk():
            raise PackageArchiveError(
                "PACKAGE_MEMBER_UNSAFE",
                "Package members must be regular files; links and special files are forbidden.",
            )
        name = member.name
        logical = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or logical.is_absolute()
            or ".." in logical.parts
            or "." in logical.parts
            or logical.as_posix() != name
            or member.size < 0
            or (name != "index.json" and member.size > MAX_MEMBER_BYTES)
        ):
            raise PackageArchiveError(
                "PACKAGE_MEMBER_UNSAFE",
                "A package member has an unsafe path, type, or size.",
            )
        folded_name = name.casefold()
        if name in exact or folded_name in folded:
            raise PackageArchiveError(
                "PACKAGE_MEMBER_DUPLICATE",
                "Duplicate or case-conflicting package member paths are forbidden.",
            )
        exact.add(name)
        folded.add(folded_name)


def _indexed_members(files: tuple[Any, ...]) -> dict[str, VerifiedArchiveMember]:
    indexed = {
        item.path: VerifiedArchiveMember(path=item.path, size=item.size, sha256=item.sha256)
        for item in files
    }
    if len(indexed) != len(files) or len({path.casefold() for path in indexed}) != len(indexed):
        raise PackageArchiveError(
            "PACKAGE_INDEX_DUPLICATE",
            "The package index contains duplicate or case-conflicting paths.",
        )
    _require_indexed_content_limit(indexed.values())
    for path in indexed:
        _require_safe_index_path(path)
    return indexed


def _legacy_indexed_members(files: list[Any]) -> dict[str, VerifiedArchiveMember]:
    result: dict[str, VerifiedArchiveMember] = {}
    try:
        for item in files:
            if not isinstance(item, Mapping):
                raise ValueError
            member = VerifiedArchiveMember.model_validate(item)
            _require_safe_index_path(member.path)
            if member.path in result or member.path.casefold() in {
                path.casefold() for path in result
            }:
                raise ValueError
            result[member.path] = member
    except (ValueError, ValidationError) as exc:
        raise PackageArchiveError(
            "PACKAGE_INDEX_INVALID",
            "The format v1 package file index is invalid.",
            exit_code=2,
        ) from exc
    _require_indexed_content_limit(result.values())
    return result


def _require_indexed_content_limit(members: Iterable[VerifiedArchiveMember]) -> None:
    if sum(member.size for member in members) > MAX_INDEXED_CONTENT_BYTES:
        raise PackageArchiveError(
            "PACKAGE_INDEX_CONTENT_LIMIT",
            "The package's indexed content exceeds the 4 MiB limit.",
        )


def _require_safe_index_path(path: str) -> None:
    logical = PurePosixPath(path)
    if (
        "\\" in path
        or logical.is_absolute()
        or ".." in logical.parts
        or "." in logical.parts
        or logical.as_posix() != path
        or path == "index.json"
    ):
        raise PackageArchiveError(
            "PACKAGE_INDEX_PATH_UNSAFE",
            "The package index contains an unsafe file path.",
        )


def _verify_indexed_content(
    archive: tarfile.TarFile,
    indexed: Mapping[str, VerifiedArchiveMember],
    actual: Mapping[str, tarfile.TarInfo],
) -> None:
    if set(actual) != set(indexed):
        raise PackageArchiveError(
            "PACKAGE_INDEX_MISMATCH",
            "The package index does not match the archive members.",
        )
    for name, member in actual.items():
        content = _read_member(archive, member)
        expected = indexed[name]
        if len(content) != expected.size or hashlib.sha256(content).hexdigest() != expected.sha256:
            raise PackageArchiveError(
                "PACKAGE_DIGEST_MISMATCH",
                "A package file does not match its indexed digest.",
            )


def _family_roots(paths: tuple[str, ...]) -> set[str]:
    roots: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] != "labs":
            raise PackageArchiveError(
                "PACKAGE_LAYOUT_INVALID",
                "Package files must stay inside one labs/<family> directory.",
            )
        roots.add("/".join(parts[:2]))
    return roots


def _load_package_yaml(content: bytes) -> LabPackageDefinition:
    try:
        documents = load_all_unique(content.decode("utf-8"))
        if len(documents) != 1:
            raise ValueError
        return LabPackageDefinition.model_validate(documents[0])
    except (UnicodeError, ValueError, ValidationError) as exc:
        raise PackageArchiveError(
            "PACKAGE_METADATA_INVALID",
            "package.yaml does not match the LabPackage contract.",
            exit_code=2,
        ) from exc


def _load_lab_yaml(content: bytes) -> LabDefinition:
    try:
        documents = load_all_unique(content.decode("utf-8"))
        if len(documents) != 1:
            raise ValueError
        return LabDefinition.model_validate(documents[0])
    except (UnicodeError, ValueError, ValidationError) as exc:
        raise PackageArchiveError(
            "PACKAGE_LAB_INVALID",
            "lab.yaml does not match the Lab contract.",
            exit_code=2,
        ) from exc


def _json_object(content: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise PackageArchiveError(
                    "PACKAGE_INDEX_DUPLICATE_KEY",
                    "Duplicate keys are forbidden in the package index.",
                    exit_code=2,
                )
            result[key] = value
        return result

    value = json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise PackageArchiveError(
            "PACKAGE_INDEX_INVALID",
            "The package index must be a JSON object.",
            exit_code=2,
        )
    return value


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    stream = archive.extractfile(member)
    if stream is None:
        raise PackageArchiveError(
            "PACKAGE_MEMBER_UNREADABLE",
            "A package member cannot be read.",
        )
    content = stream.read(member.size + 1)
    if len(content) != member.size:
        raise PackageArchiveError(
            "PACKAGE_MEMBER_SIZE_MISMATCH",
            "A package member size does not match its archive header.",
        )
    return content


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_owned_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False):
        current_path = Path(current)
        for name in files:
            (current_path / name).unlink()
        for name in directories:
            (current_path / name).rmdir()
    root.rmdir()


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_INDEX_BYTES",
    "MAX_INDEXED_CONTENT_BYTES",
    "MAX_MEMBER_BYTES",
    "PackageArchiveError",
    "VerifiedArchiveMember",
    "VerifiedLabArchive",
    "extract_verified_archive",
    "verify_extracted_content",
    "verify_lab_archive",
]
