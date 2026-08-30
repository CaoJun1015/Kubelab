"""Verify KubeLab wheel and source distributions without third-party packages."""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from email import parser
from pathlib import Path, PurePosixPath
from typing import Protocol

EXPECTED_LAB_COUNT = 21
EXPECTED_VARIANT_COUNT = 12
EXPECTED_WEB_ASSETS = {
    "static/app.js",
    "static/styles.css",
    "templates/base.html",
    "templates/dashboard.html",
    "templates/lab_detail.html",
    "templates/labs.html",
    "templates/onboarding.html",
    "templates/progress.html",
    "templates/session.html",
}
EXPECTED_PACKAGE_FILES = {
    "migrations/env.py",
    "migrations/script.py.mako",
    "migrations/versions/0001_initial_persistence.py",
    "migrations/versions/0002_guided_learning.py",
    "migrations/versions/0003_lab_variants.py",
    "py.typed",
}
EXPECTED_PROJECT_DOCS = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "docs/ARCHITECTURE.md",
    "docs/LAB_DEVELOPMENT.md",
    "docs/example-retrospective.md",
}
EXPECTED_SCREENSHOTS = {
    "docs/assets/dashboard.jpg",
    "docs/assets/labs.jpg",
    "docs/assets/mobile-dashboard-390.jpg",
    "docs/assets/progress.jpg",
    "docs/assets/session.jpg",
}
FORBIDDEN_PART_PREFIXES = (
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
)
FORBIDDEN_SUFFIXES = (".db", ".db-shm", ".db-wal", ".log", ".pyc", ".pyo")
FORBIDDEN_CONTENT = {
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "Kubernetes bearer token": re.compile(
        rb"(?im)^\s*token\s*:\s*[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\s*$"
    ),
    "Windows user path": re.compile(rb"(?i)[A-Z]:\\Users\\[^\\\r\n]+"),
    "repository machine path": re.compile(rb"(?i)(?:[A-Z]:\\ChatGPT\\|/mnt/[a-z]/ChatGPT/)"),
    "Linux user path": re.compile(rb"(?i)/home/" rb"cj/"),
}


class ArchiveReader(Protocol):
    def names(self) -> list[str]: ...

    def read(self, name: str) -> bytes: ...


@dataclass
class ZipReader:
    archive: zipfile.ZipFile

    def names(self) -> list[str]:
        return [name for name in self.archive.namelist() if not name.endswith("/")]

    def read(self, name: str) -> bytes:
        return self.archive.read(name)


@dataclass
class TarReader:
    archive: tarfile.TarFile

    def names(self) -> list[str]:
        return [member.name for member in self.archive.getmembers() if member.isfile()]

    def read(self, name: str) -> bytes:
        member = self.archive.getmember(name)
        stream = self.archive.extractfile(member)
        if stream is None:
            raise ValueError(f"cannot read archive member: {name}")
        return stream.read()


def _normalise(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return path


def _relative_sdist_names(names: list[str]) -> dict[str, str]:
    roots = {path.parts[0] for name in names if (path := _normalise(name)).parts}
    if len(roots) != 1:
        raise ValueError("sdist must contain exactly one top-level project directory")
    result: dict[str, str] = {}
    for name in names:
        path = _normalise(name)
        if len(path.parts) > 1:
            result[PurePosixPath(*path.parts[1:]).as_posix()] = name
    return result


def _assert_no_forbidden_files(names: list[str], label: str) -> None:
    failures: list[str] = []
    for name in names:
        path = _normalise(name)
        lowered = tuple(part.lower() for part in path.parts)
        if any(part.startswith(prefix) for part in lowered for prefix in FORBIDDEN_PART_PREFIXES):
            failures.append(name)
        if lowered and lowered[-1].endswith(FORBIDDEN_SUFFIXES):
            failures.append(name)
        if any(part in {"build", "dist"} for part in lowered):
            failures.append(name)
        if lowered and lowered[-1] in {"config.toml", "kubeconfig", "credentials"}:
            failures.append(name)
    if failures:
        raise ValueError(f"{label} contains forbidden files: {sorted(set(failures))}")


def _assert_no_sensitive_content(reader: ArchiveReader, names: list[str], label: str) -> None:
    text_suffixes = {
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".mako",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    failures: list[str] = []
    for name in names:
        if _normalise(name).suffix.lower() not in text_suffixes:
            continue
        content = reader.read(name)
        for description, pattern in FORBIDDEN_CONTENT.items():
            if pattern.search(content):
                failures.append(f"{name} ({description})")
    if failures:
        raise ValueError(f"{label} contains sensitive or machine-local content: {failures}")


def _metadata(reader: ArchiveReader, names: list[str], suffix: str) -> bytes:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {suffix}, found {matches}")
    return reader.read(matches[0])


def _assert_metadata(content: bytes, expected_version: str, label: str) -> None:
    metadata = parser.BytesParser().parsebytes(content)
    if metadata["Name"] != "kubelab":
        raise ValueError(f"{label} metadata Name is not kubelab")
    if metadata["Version"] != expected_version:
        raise ValueError(
            f"{label} metadata version {metadata['Version']!r} != {expected_version!r}"
        )
    if metadata["Author"] != "CaoJun":
        raise ValueError(f"{label} metadata Author is not CaoJun")
    if metadata["Author-email"]:
        raise ValueError(f"{label} metadata unexpectedly exposes an author email")
    if "text/markdown" not in (metadata["Description-Content-Type"] or ""):
        raise ValueError(f"{label} metadata does not embed the README as Markdown")


def verify_wheel(path: Path, expected_version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        reader = ZipReader(archive)
        names = reader.names()
        _assert_no_forbidden_files(names, "wheel")
        _assert_no_sensitive_content(reader, names, "wheel")
        labs = {
            name.split("/")[2]
            for name in names
            if re.fullmatch(r"kubelab/labs/[^/]+/lab\.yaml", name)
        }
        if len(labs) != EXPECTED_LAB_COUNT:
            raise ValueError(f"wheel contains {len(labs)} labs, expected {EXPECTED_LAB_COUNT}")
        variants = {
            name
            for name in names
            if re.fullmatch(r"kubelab/labs/[^/]+/variants/[^/]+/variant\.yaml", name)
        }
        if len(variants) != EXPECTED_VARIANT_COUNT:
            raise ValueError(
                f"wheel contains {len(variants)} variants, expected {EXPECTED_VARIANT_COUNT}"
            )
        for asset in EXPECTED_WEB_ASSETS | EXPECTED_PACKAGE_FILES:
            if f"kubelab/{asset}" not in names:
                raise ValueError(f"wheel is missing kubelab/{asset}")
        wheel_docs = {
            "kubelab/docs/README.md",
            "kubelab/docs/LICENSE",
            "kubelab/docs/CONTRIBUTING.md",
            "kubelab/docs/SECURITY.md",
            "kubelab/docs/CHANGELOG.md",
            "kubelab/docs/project/ARCHITECTURE.md",
            "kubelab/docs/project/LAB_DEVELOPMENT.md",
            "kubelab/docs/project/example-retrospective.md",
        }
        wheel_docs.update(
            f"kubelab/docs/project/{path.removeprefix('docs/')}" for path in EXPECTED_SCREENSHOTS
        )
        missing_docs = wheel_docs.difference(names)
        if missing_docs:
            raise ValueError(f"wheel is missing documentation: {sorted(missing_docs)}")
        _assert_metadata(_metadata(reader, names, ".dist-info/METADATA"), expected_version, "wheel")


def verify_sdist(path: Path, expected_version: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        reader = TarReader(archive)
        names = reader.names()
        _assert_no_forbidden_files(names, "sdist")
        _assert_no_sensitive_content(reader, names, "sdist")
        relative = _relative_sdist_names(names)
        labs = {
            name.split("/")[1] for name in relative if re.fullmatch(r"labs/[^/]+/lab\.yaml", name)
        }
        if len(labs) != EXPECTED_LAB_COUNT:
            raise ValueError(f"sdist contains {len(labs)} labs, expected {EXPECTED_LAB_COUNT}")
        variants = {
            name
            for name in relative
            if re.fullmatch(r"labs/[^/]+/variants/[^/]+/variant\.yaml", name)
        }
        if len(variants) != EXPECTED_VARIANT_COUNT:
            raise ValueError(
                f"sdist contains {len(variants)} variants, expected {EXPECTED_VARIANT_COUNT}"
            )
        for asset in EXPECTED_WEB_ASSETS | EXPECTED_PACKAGE_FILES:
            if f"src/kubelab/{asset}" not in relative:
                raise ValueError(f"sdist is missing src/kubelab/{asset}")
        missing_docs = (EXPECTED_PROJECT_DOCS | EXPECTED_SCREENSHOTS).difference(relative)
        if missing_docs:
            raise ValueError(f"sdist is missing documentation: {sorted(missing_docs)}")
        _assert_metadata(_metadata(reader, names, "/PKG-INFO"), expected_version, "sdist")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        verify_wheel(args.wheel, args.version)
        verify_sdist(args.sdist, args.version)
    except (KeyError, OSError, tarfile.TarError, ValueError, zipfile.BadZipFile) as exc:
        print(f"distribution verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"verified KubeLab {args.version}: {EXPECTED_LAB_COUNT} labs, "
        f"{EXPECTED_VARIANT_COUNT} variants, "
        f"{len(EXPECTED_WEB_ASSETS)} Web assets, metadata and safety rules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
