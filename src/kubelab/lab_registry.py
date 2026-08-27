"""Deterministic, isolated loading of local KubeLab experiment packages."""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from kubelab.lab_schema import LabDefinition, LabMetadata, LabModel
from kubelab.manifest_security import ManifestDocument, ManifestSecurityScanner


class RegistryErrorCode(StrEnum):
    LAB_YAML_INVALID = "LAB_YAML_INVALID"
    LAB_SCHEMA_INVALID = "LAB_SCHEMA_INVALID"
    LAB_DUPLICATE_ID = "LAB_DUPLICATE_ID"
    LAB_PATH_ESCAPE = "LAB_PATH_ESCAPE"
    LAB_MANIFEST_MISSING = "LAB_MANIFEST_MISSING"
    MANIFEST_YAML_INVALID = "MANIFEST_YAML_INVALID"
    MANIFEST_KIND_UNSUPPORTED = "MANIFEST_KIND_UNSUPPORTED"
    MANIFEST_CLUSTER_SCOPED = "MANIFEST_CLUSTER_SCOPED"
    MANIFEST_NAMESPACE_FORBIDDEN = "MANIFEST_NAMESPACE_FORBIDDEN"
    MANIFEST_UNSAFE = "MANIFEST_UNSAFE"
    LABS_DIR_INVALID = "LABS_DIR_INVALID"
    LAB_SOURCE_CHANGED = "LAB_SOURCE_CHANGED"


class LabMaterializationError(RuntimeError):
    """Raised when a previously loaded lab can no longer be materialized safely."""

    code = "LAB_SOURCE_CHANGED"

    def __init__(self, errors: tuple[RegistryError, ...]) -> None:
        super().__init__("The lab source changed after registry validation.")
        self.errors = errors


class LoadedLab(LabModel):
    """A validated lab and redacted source metadata safe for callers to inspect."""

    definition: LabDefinition
    lab_path: str
    manifest_paths: tuple[str, ...]
    manifest_sha256: tuple[str, ...]


class RegistryError(LabModel):
    """Stable, redacted loading error for one lab source."""

    code: RegistryErrorCode
    message: str
    lab_path: str
    field_path: str | None = None
    retryable: bool = False
    lab_id: str | None = None


class RegistrySnapshot(LabModel):
    """One deterministic view of all accepted and rejected labs."""

    labs: tuple[LoadedLab, ...]
    errors: tuple[RegistryError, ...]


@dataclass(frozen=True)
class _MaterializedLab:
    """Rescanned Manifest documents for immediate use by the cluster gateway.

    This deliberately is not a Pydantic/public DTO because it contains raw Manifest
    mappings, including possible Secret values. It must never cross a CLI or Web boundary.
    """

    documents: tuple[ManifestDocument, ...]


@dataclass(frozen=True)
class _LabCandidate:
    definition: LabDefinition
    lab_file: Path
    lab_path: str
    lab_dir: Path


@dataclass(frozen=True)
class _ManifestBundle:
    documents: tuple[ManifestDocument, ...]
    paths: tuple[str, ...]
    digests: tuple[str, ...]
    errors: tuple[RegistryError, ...]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that treats duplicate mapping keys as invalid input."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class LabRegistry:
    """Load local lab definitions without executing commands or touching a cluster."""

    def __init__(
        self,
        labs_dir: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        scanner: ManifestSecurityScanner | None = None,
    ) -> None:
        self._explicit_labs_dir = labs_dir
        self._environ = environ if environ is not None else os.environ
        self._scanner = scanner or ManifestSecurityScanner()

    def scan(self) -> RegistrySnapshot:
        """Return all valid labs and isolated, redacted source errors."""
        root, root_error = self._resolve_root()
        if root_error is not None or root is None:
            return RegistrySnapshot(labs=(), errors=(root_error,) if root_error else ())

        try:
            lab_files = sorted(
                root.rglob("lab.yaml"),
                key=lambda path: (
                    path.relative_to(root).as_posix().casefold(),
                    path.relative_to(root).as_posix(),
                ),
            )
        except OSError:
            return RegistrySnapshot(
                labs=(),
                errors=(
                    RegistryError(
                        code=RegistryErrorCode.LABS_DIR_INVALID,
                        message="The labs directory could not be scanned.",
                        lab_path=".",
                        retryable=True,
                    ),
                ),
            )

        candidates: list[_LabCandidate] = []
        errors: list[RegistryError] = []
        for lab_file in lab_files:
            candidate, candidate_errors = self._load_definition(root, lab_file)
            errors.extend(candidate_errors)
            if candidate is not None:
                candidates.append(candidate)

        duplicates: dict[str, list[_LabCandidate]] = defaultdict(list)
        for candidate in candidates:
            duplicates[candidate.definition.metadata.id].append(candidate)
        duplicate_ids = {lab_id for lab_id, group in duplicates.items() if len(group) > 1}
        for lab_id in sorted(duplicate_ids):
            for candidate in duplicates[lab_id]:
                errors.append(
                    RegistryError(
                        code=RegistryErrorCode.LAB_DUPLICATE_ID,
                        message="The lab ID is duplicated in the registry.",
                        lab_path=candidate.lab_path,
                        field_path="metadata.id",
                        lab_id=lab_id,
                    )
                )

        loaded: list[LoadedLab] = []
        for candidate in candidates:
            if candidate.definition.metadata.id in duplicate_ids:
                continue
            bundle = self._load_manifests(root, candidate)
            errors.extend(bundle.errors)
            if bundle.errors:
                continue
            issues = self._scanner.scan(
                bundle.documents,
                namespace=candidate.definition.environment.namespace,
            )
            if issues:
                errors.extend(
                    RegistryError(
                        code=RegistryErrorCode(issue.code),
                        message=issue.message,
                        lab_path=issue.manifest_path,
                        field_path=issue.field_path,
                        lab_id=candidate.definition.metadata.id,
                    )
                    for issue in issues
                )
                continue
            loaded.append(
                LoadedLab(
                    definition=candidate.definition,
                    lab_path=candidate.lab_path,
                    manifest_paths=bundle.paths,
                    manifest_sha256=bundle.digests,
                )
            )

        loaded.sort(key=lambda lab: (lab.lab_path.casefold(), lab.lab_path))
        errors.sort(
            key=lambda error: (
                error.lab_path.casefold(),
                error.lab_path,
                error.field_path or "",
                error.code.value,
            )
        )
        return RegistrySnapshot(labs=tuple(loaded), errors=tuple(errors))

    def materialize_for_gateway(self, loaded: LoadedLab) -> _MaterializedLab:
        """Re-read, digest-check, and rescan a loaded lab immediately before apply."""
        root, root_error = self._resolve_root()
        if root_error is not None or root is None:
            raise LabMaterializationError((root_error,) if root_error is not None else ())

        if (
            not _portable_relative_path(loaded.lab_path)
            or PurePosixPath(loaded.lab_path).name != "lab.yaml"
        ):
            raise LabMaterializationError((self._source_changed(loaded),))
        lab_file = root.joinpath(*PurePosixPath(loaded.lab_path).parts)
        try:
            resolved_lab_file = lab_file.resolve(strict=True)
        except OSError:
            raise LabMaterializationError((self._source_changed(loaded),)) from None
        if not _is_within(resolved_lab_file, root):
            raise LabMaterializationError((self._source_changed(loaded),))
        candidate, definition_errors = self._load_definition(root, lab_file)
        if candidate is None or definition_errors:
            raise LabMaterializationError(definition_errors)
        if candidate.definition != loaded.definition or candidate.lab_path != loaded.lab_path:
            raise LabMaterializationError((self._source_changed(loaded),))

        bundle = self._load_manifests(root, candidate)
        if bundle.errors:
            raise LabMaterializationError(bundle.errors)
        if bundle.paths != loaded.manifest_paths or bundle.digests != loaded.manifest_sha256:
            raise LabMaterializationError((self._source_changed(loaded),))

        issues = self._scanner.scan(
            bundle.documents,
            namespace=candidate.definition.environment.namespace,
        )
        if issues:
            errors = tuple(
                RegistryError(
                    code=RegistryErrorCode(issue.code),
                    message=issue.message,
                    lab_path=issue.manifest_path,
                    field_path=issue.field_path,
                    lab_id=candidate.definition.metadata.id,
                )
                for issue in issues
            )
            raise LabMaterializationError(errors)
        return _MaterializedLab(bundle.documents)

    @staticmethod
    def _source_changed(loaded: LoadedLab) -> RegistryError:
        return RegistryError(
            code=RegistryErrorCode.LAB_SOURCE_CHANGED,
            message="Lab files changed after registry validation; reload the registry.",
            lab_path=loaded.lab_path,
            retryable=True,
            lab_id=loaded.definition.metadata.id,
        )

    def _resolve_root(self) -> tuple[Path | None, RegistryError | None]:
        if self._explicit_labs_dir is not None:
            candidate = self._explicit_labs_dir.expanduser()
        else:
            override = self._environ.get("KUBELAB_LABS_DIR")
            if override:
                candidate = Path(override).expanduser()
                if not candidate.is_absolute():
                    return None, self._root_error("KUBELAB_LABS_DIR must be an absolute path.")
            else:
                package_root = Path(__file__).resolve().parent / "labs"
                project_root = Path(__file__).resolve().parents[2] / "labs"
                candidate = package_root if package_root.is_dir() else project_root
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            return None, self._root_error("The labs directory does not exist or is inaccessible.")
        if not root.is_dir():
            return None, self._root_error("The labs path is not a directory.")
        return root, None

    @staticmethod
    def _root_error(message: str) -> RegistryError:
        return RegistryError(
            code=RegistryErrorCode.LABS_DIR_INVALID,
            message=message,
            lab_path=".",
            retryable=True,
        )

    def _load_definition(
        self, root: Path, lab_file: Path
    ) -> tuple[_LabCandidate | None, tuple[RegistryError, ...]]:
        lab_path = _relative_display(lab_file, root)
        lab_dir = lab_file.parent.resolve()
        try:
            resolved_lab_file = lab_file.resolve(strict=True)
        except OSError:
            return None, (
                RegistryError(
                    code=RegistryErrorCode.LAB_YAML_INVALID,
                    message="lab.yaml is inaccessible.",
                    lab_path=lab_path,
                    retryable=True,
                ),
            )
        if not _is_within(resolved_lab_file, lab_dir) or not resolved_lab_file.is_file():
            return None, (
                RegistryError(
                    code=RegistryErrorCode.LAB_PATH_ESCAPE,
                    message="lab.yaml must resolve to a regular file in its experiment directory.",
                    lab_path=lab_path,
                ),
            )
        try:
            text = resolved_lab_file.read_text(encoding="utf-8")
            documents = list(yaml.load_all(text, Loader=_UniqueKeyLoader))
        except UnicodeError:
            return None, (self._yaml_error(lab_path, "lab.yaml must be UTF-8."),)
        except yaml.YAMLError as exc:
            return None, (self._yaml_error(lab_path, _safe_yaml_message(exc, "Lab YAML")),)
        except OSError:
            return None, (
                RegistryError(
                    code=RegistryErrorCode.LAB_YAML_INVALID,
                    message="lab.yaml could not be read.",
                    lab_path=lab_path,
                    retryable=True,
                ),
            )
        if len(documents) != 1 or not isinstance(documents[0], Mapping):
            return None, (
                self._yaml_error(lab_path, "lab.yaml must contain exactly one mapping document."),
            )
        try:
            definition = LabDefinition.model_validate(documents[0])
        except ValidationError as exc:
            validation_errors = tuple(
                RegistryError(
                    code=RegistryErrorCode.LAB_SCHEMA_INVALID,
                    message="Lab schema validation failed.",
                    lab_path=lab_path,
                    field_path=_validation_path(error["loc"]),
                )
                for error in exc.errors(include_input=False, include_url=False)
            )
            return None, validation_errors
        return _LabCandidate(definition, resolved_lab_file, lab_path, lab_dir), ()

    @staticmethod
    def _yaml_error(lab_path: str, message: str) -> RegistryError:
        return RegistryError(
            code=RegistryErrorCode.LAB_YAML_INVALID,
            message=message,
            lab_path=lab_path,
        )

    def _load_manifests(self, root: Path, candidate: _LabCandidate) -> _ManifestBundle:
        documents: list[ManifestDocument] = []
        paths: list[str] = []
        digests: list[str] = []
        errors: list[RegistryError] = []
        for manifest_reference in candidate.definition.environment.manifests:
            manifest, display_path, path_error = self._resolve_manifest_path(
                root, candidate, manifest_reference
            )
            if path_error is not None or manifest is None or display_path is None:
                if path_error is not None:
                    errors.append(path_error)
                continue
            try:
                payload = manifest.read_bytes()
                text = payload.decode("utf-8")
                parsed = list(yaml.load_all(text, Loader=_UniqueKeyLoader))
            except UnicodeError:
                errors.append(
                    self._manifest_yaml_error(candidate, display_path, "Manifest must be UTF-8.")
                )
                continue
            except yaml.YAMLError as exc:
                errors.append(
                    self._manifest_yaml_error(
                        candidate,
                        display_path,
                        _safe_yaml_message(exc, "Manifest YAML"),
                    )
                )
                continue
            except OSError:
                errors.append(
                    RegistryError(
                        code=RegistryErrorCode.LAB_MANIFEST_MISSING,
                        message="Manifest could not be read.",
                        lab_path=display_path,
                        retryable=True,
                        lab_id=candidate.definition.metadata.id,
                    )
                )
                continue
            if not parsed or any(not isinstance(document, Mapping) for document in parsed):
                errors.append(
                    self._manifest_yaml_error(
                        candidate,
                        display_path,
                        "Every Manifest YAML document must be a non-empty mapping.",
                    )
                )
                continue
            paths.append(manifest_reference)
            digests.append(hashlib.sha256(payload).hexdigest())
            documents.extend(
                ManifestDocument(display_path, index, document)
                for index, document in enumerate(parsed)
            )
        return _ManifestBundle(tuple(documents), tuple(paths), tuple(digests), tuple(errors))

    def _resolve_manifest_path(
        self,
        root: Path,
        candidate: _LabCandidate,
        reference: str,
    ) -> tuple[Path | None, str | None, RegistryError | None]:
        lab_id = candidate.definition.metadata.id
        if not _portable_relative_path(reference):
            return (
                None,
                None,
                RegistryError(
                    code=RegistryErrorCode.LAB_PATH_ESCAPE,
                    message="Manifest paths must be portable relative paths without traversal.",
                    lab_path=candidate.lab_path,
                    field_path="environment.manifests",
                    lab_id=lab_id,
                ),
            )
        relative = PurePosixPath(reference)
        unresolved = candidate.lab_dir.joinpath(*relative.parts)
        display_path = _relative_display(unresolved, root)
        try:
            resolved = unresolved.resolve(strict=True)
        except OSError:
            return (
                None,
                None,
                RegistryError(
                    code=RegistryErrorCode.LAB_MANIFEST_MISSING,
                    message="A declared Manifest file does not exist.",
                    lab_path=display_path,
                    field_path="environment.manifests",
                    lab_id=lab_id,
                ),
            )
        if not _is_within(resolved, candidate.lab_dir):
            return (
                None,
                None,
                RegistryError(
                    code=RegistryErrorCode.LAB_PATH_ESCAPE,
                    message="Manifest path resolves outside its experiment directory.",
                    lab_path=display_path,
                    field_path="environment.manifests",
                    lab_id=lab_id,
                ),
            )
        if not resolved.is_file():
            return (
                None,
                None,
                RegistryError(
                    code=RegistryErrorCode.LAB_MANIFEST_MISSING,
                    message="A declared Manifest is not a regular file.",
                    lab_path=display_path,
                    field_path="environment.manifests",
                    lab_id=lab_id,
                ),
            )
        return resolved, display_path, None

    @staticmethod
    def _manifest_yaml_error(
        candidate: _LabCandidate, display_path: str, message: str
    ) -> RegistryError:
        return RegistryError(
            code=RegistryErrorCode.MANIFEST_YAML_INVALID,
            message=message,
            lab_path=display_path,
            field_path="documents",
            lab_id=candidate.definition.metadata.id,
        )


def _portable_relative_path(value: str) -> bool:
    if value != value.strip() or "\\" in value:
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    raw_parts = value.split("/")
    return not (
        not value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in raw_parts)
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _validation_path(location: tuple[int | str, ...]) -> str:
    rendered = ""
    for part in location:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += ("." if rendered else "") + part
    return rendered or "$"


def _safe_yaml_message(error: yaml.YAMLError, label: str) -> str:
    mark = getattr(error, "problem_mark", None)
    if mark is not None:
        return f"{label} is invalid at line {mark.line + 1}, column {mark.column + 1}."
    return f"{label} is invalid."


__all__ = [
    "LabMetadata",
    "LabDefinition",
    "LabRegistry",
    "LabMaterializationError",
    "LoadedLab",
    "RegistryError",
    "RegistryErrorCode",
    "RegistrySnapshot",
]
