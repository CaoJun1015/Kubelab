"""Offline package archive integrity and extraction boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from kubelab.authoring import AuthoringService
from kubelab.package_archive import (
    MAX_ARCHIVE_BYTES,
    PackageArchiveError,
    extract_verified_archive,
    verify_lab_archive,
)


def _package(tmp_path: Path) -> Path:
    service = AuthoringService(tmp_path)
    family = tmp_path / "lab-local-archive"
    initialized = service.init(
        family,
        scenario_type="baseline",
        scenario_id="lab-local-archive",
        title="本地归档",
        category="workload",
        difficulty="intermediate",
        description="验证可信本地归档边界。",
    )
    assert initialized.passed
    output = tmp_path / "lab-local-archive-0.1.0.kubelab-lab.tar.gz"
    report = service.package(family, output=output)
    assert report.passed, report.issues
    return output


def _contents(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
            for member in archive.getmembers()
        }


def _write_archive(path: Path, contents: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_v2_archive_verifies_and_extracts_only_indexed_content(tmp_path: Path) -> None:
    archive = _package(tmp_path)

    verified = verify_lab_archive(archive, kubelab_version="0.6.0a0")
    destination = tmp_path / "content"
    extract_verified_archive(archive, verified, destination)

    assert verified.importable
    assert verified.compatible
    assert verified.format_version == 2
    assert verified.lab_id == "lab-local-archive"
    assert verified.package_version == "0.1.0"
    assert verified.publisher_id == "local-author"
    assert (destination / verified.family_directory / "lab.yaml").is_file()
    assert not (destination / "index.json").exists()


def test_format_v1_is_integrity_checked_but_not_importable(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    contents = _contents(archive)
    index = json.loads(contents["index.json"])
    index["formatVersion"] = 1
    index.pop("package")
    index["schemaVersions"].pop("package")
    contents["index.json"] = json.dumps(index).encode()
    legacy = tmp_path / "legacy.kubelab-lab.tar.gz"
    _write_archive(legacy, contents)

    verified = verify_lab_archive(legacy)

    assert verified.format_version == 1
    assert not verified.importable
    assert "Rebuild" in verified.compatibility_message


def test_digest_tampering_and_metadata_disagreement_are_rejected(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    contents = _contents(archive)
    lab_path = next(name for name in contents if name.endswith("/lab.yaml"))
    contents[lab_path] += b"\n# changed\n"
    tampered = tmp_path / "tampered.kubelab-lab.tar.gz"
    _write_archive(tampered, contents)

    with pytest.raises(PackageArchiveError) as digest_error:
        verify_lab_archive(tampered)
    assert digest_error.value.code == "PACKAGE_DIGEST_MISMATCH"

    contents = _contents(archive)
    index = json.loads(contents["index.json"])
    index["package"]["publisherName"] = "Another Publisher"
    contents["index.json"] = json.dumps(index).encode()
    mismatch = tmp_path / "mismatch.kubelab-lab.tar.gz"
    _write_archive(mismatch, contents)
    with pytest.raises(PackageArchiveError) as metadata_error:
        verify_lab_archive(mismatch)
    assert metadata_error.value.code == "PACKAGE_METADATA_MISMATCH"


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "labs\\escape"])
def test_unsafe_member_paths_are_rejected(tmp_path: Path, unsafe_name: str) -> None:
    archive = tmp_path / "unsafe.kubelab-lab.tar.gz"
    _write_archive(archive, {unsafe_name: b"x", "index.json": b"{}"})

    with pytest.raises(PackageArchiveError) as caught:
        verify_lab_archive(archive)

    assert caught.value.code == "PACKAGE_MEMBER_UNSAFE"


def test_links_devices_and_case_conflicting_members_are_rejected(tmp_path: Path) -> None:
    link_archive = tmp_path / "link.kubelab-lab.tar.gz"
    with tarfile.open(link_archive, "w:gz") as archive:
        link = tarfile.TarInfo("labs/family/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
        index = tarfile.TarInfo("index.json")
        index.size = 2
        archive.addfile(index, io.BytesIO(b"{}"))
    with pytest.raises(PackageArchiveError, match="regular files"):
        verify_lab_archive(link_archive)

    duplicate = tmp_path / "duplicate.kubelab-lab.tar.gz"
    _write_archive(
        duplicate,
        {
            "labs/family/Lab.yaml": b"a",
            "labs/family/lab.yaml": b"b",
            "index.json": b"{}",
        },
    )
    with pytest.raises(PackageArchiveError) as caught:
        verify_lab_archive(duplicate)
    assert caught.value.code == "PACKAGE_MEMBER_DUPLICATE"


def test_compatibility_is_public_but_blocks_importability(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    contents = _contents(archive)
    package_path = next(name for name in contents if name.endswith("/package.yaml"))
    metadata = yaml.safe_load(contents[package_path])
    metadata["spec"]["requiresKubelab"] = ">=9.0,<10"
    contents[package_path] = yaml.safe_dump(metadata, sort_keys=False).encode()
    index = json.loads(contents["index.json"])
    index["package"]["requiresKubelab"] = ">=9.0,<10"
    indexed = next(item for item in index["files"] if item["path"] == package_path)
    indexed["size"] = len(contents[package_path])
    indexed["sha256"] = hashlib.sha256(contents[package_path]).hexdigest()
    contents["index.json"] = json.dumps(index).encode()
    incompatible = tmp_path / "incompatible.kubelab-lab.tar.gz"
    _write_archive(incompatible, contents)

    verified = verify_lab_archive(incompatible, kubelab_version="0.6.0a0")

    assert not verified.compatible
    assert not verified.importable
    assert "different" in verified.compatibility_message


def test_source_change_and_compressed_size_limit_fail_closed(tmp_path: Path) -> None:
    archive = _package(tmp_path)
    verified = verify_lab_archive(archive)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(PackageArchiveError) as changed:
        extract_verified_archive(archive, verified, tmp_path / "changed-content")
    assert changed.value.code == "PACKAGE_ARCHIVE_CHANGED"

    oversized = tmp_path / "oversized.kubelab-lab.tar.gz"
    oversized.write_bytes(b"x" * (MAX_ARCHIVE_BYTES + 1))
    with pytest.raises(PackageArchiveError) as large:
        verify_lab_archive(oversized)
    assert large.value.code == "PACKAGE_ARCHIVE_TOO_LARGE"
