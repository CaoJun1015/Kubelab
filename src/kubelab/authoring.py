"""Safe local services for creating, reviewing, and proving KubeLab content."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kubelab.authoring_fake import FakeContractError, FakeContractRunner
from kubelab.authoring_schema import LabAuthoringContract, RepairPlan
from kubelab.authoring_templates import baseline_template, composite_template, variant_template
from kubelab.lab_registry import EffectiveLab, ExecutableLab, LabRegistry, LoadedLab, LoadedVariant
from kubelab.manifest_security import ManifestDocument, ManifestSecurityScanner
from kubelab.public_projection import project_variant_disclosure
from kubelab.safe_yaml import load_all_unique

_MAX_AUTHORING_BYTES = 256 * 1024
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_MANIFEST_DOCUMENTS = 50
_MAX_PACKAGE_BYTES = 4 * 1024 * 1024
_MAX_PACKAGE_FILES = 256
_RECREATE_KINDS = frozenset({"Job", "Service", "DaemonSet", "StatefulSet", "PersistentVolumeClaim"})
_SHELL_TOKENS = (";", "||", "`", "$(", ">", "<")


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class AuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class AuthoringIssue(AuthoringModel):
    code: str
    severity: IssueSeverity
    relative_path: str = Field(alias="relativePath")
    field_path: str | None = Field(alias="fieldPath", default=None)
    message: str
    docs_anchor: str = Field(alias="docsAnchor")
    exit_code: int = Field(exclude=True, ge=2, le=10)


class ScenarioResult(AuthoringModel):
    scenario: str
    scenario_type: str = Field(alias="scenarioType")
    passed: bool


class AuthoringLintReport(AuthoringModel):
    command: str = "lab lint"
    passed: bool
    scenarios: tuple[str, ...]
    issues: tuple[AuthoringIssue, ...]
    error_count: int = Field(alias="errorCount", ge=0)
    warning_count: int = Field(alias="warningCount", ge=0)

    @property
    def exit_code(self) -> int:
        return _issues_exit_code(self.issues)


class AuthoringInitReport(AuthoringModel):
    command: str = "lab init"
    passed: bool
    scenario_type: str = Field(alias="scenarioType")
    target: str
    files: tuple[str, ...]
    dry_run: bool = Field(alias="dryRun")
    issues: tuple[AuthoringIssue, ...] = ()

    @property
    def exit_code(self) -> int:
        return _issues_exit_code(self.issues)


class AuthoringTestReport(AuthoringModel):
    command: str = "lab test"
    passed: bool
    results: tuple[ScenarioResult, ...]
    issues: tuple[AuthoringIssue, ...]
    error_count: int = Field(alias="errorCount", ge=0)

    @property
    def exit_code(self) -> int:
        return _issues_exit_code(self.issues)


class AuthoringResourceSummary(AuthoringModel):
    api_version: str = Field(alias="apiVersion")
    kind: str
    name: str


class DisclosurePreview(AuthoringModel):
    revealed: bool
    task: str
    completion_description: str = Field(alias="completionDescription")
    check_types: tuple[str, ...] = Field(alias="checkTypes")
    scenario_name: str | None = Field(alias="scenarioName", default=None)
    scenario_description: str | None = Field(alias="scenarioDescription", default=None)
    key_evidence: str | None = Field(alias="keyEvidence", default=None)
    root_cause: str | None = Field(alias="rootCause", default=None)
    resolution: str | None = None
    prevention: str | None = None


class RepairSummary(AuthoringModel):
    stage: str
    manifest: str
    changes: tuple[str, ...]


class ScenarioInspection(AuthoringModel):
    scenario: str
    scenario_type: str = Field(alias="scenarioType")
    inherited_from: str | None = Field(alias="inheritedFrom", default=None)
    resources: tuple[AuthoringResourceSummary, ...]
    images: tuple[str, ...]
    workspace_permissions: Literal["namespace-restricted"] = Field(
        alias="workspacePermissions", default="namespace-restricted"
    )
    initial_check_types: tuple[str, ...] = Field(alias="initialCheckTypes")
    success_check_types: tuple[str, ...] = Field(alias="successCheckTypes")
    repairs: tuple[RepairSummary, ...]
    before_pass: DisclosurePreview = Field(alias="beforePass")
    after_pass: DisclosurePreview = Field(alias="afterPass")
    files: tuple[str, ...]
    file_sha256: dict[str, str] = Field(alias="fileSha256")


class AuthoringInspectReport(AuthoringModel):
    command: str = "lab inspect"
    passed: bool
    scenarios: tuple[ScenarioInspection, ...]
    issues: tuple[AuthoringIssue, ...]
    error_count: int = Field(alias="errorCount", ge=0)
    warning_count: int = Field(alias="warningCount", ge=0)

    @property
    def exit_code(self) -> int:
        return _issues_exit_code(self.issues)


class AuthoringPackageReport(AuthoringModel):
    command: str = "lab package"
    passed: bool
    lab_id: str | None = Field(alias="labId", default=None)
    output: str | None = None
    sha256: str | None = None
    file_count: int = Field(alias="fileCount", ge=0)
    issues: tuple[AuthoringIssue, ...]

    @property
    def exit_code(self) -> int:
        return _issues_exit_code(self.issues)


@dataclass(frozen=True)
class _Scenario:
    scenario_id: str
    scenario_type: str
    directory: Path
    parent: LoadedLab
    executable: ExecutableLab
    variant: LoadedVariant | None
    contract: LabAuthoringContract


@dataclass(frozen=True)
class _Target:
    path: Path
    catalog_root: Path
    family_directory: Path | None
    variant_directory: Path | None


@dataclass(frozen=True)
class _LoadedTarget:
    target: _Target
    registry: LabRegistry
    scenarios: tuple[_Scenario, ...]
    issues: tuple[AuthoringIssue, ...]


class AuthoringService:
    """Compose runtime schemas and validators without building the learner runtime."""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        scanner: ManifestSecurityScanner | None = None,
        fake_runner: FakeContractRunner | None = None,
    ) -> None:
        root = (workspace or Path.cwd()).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("The author workspace must be a directory.")
        self._workspace = root
        self._scanner = scanner or ManifestSecurityScanner()
        self._fake_runner = fake_runner or FakeContractRunner()

    def init(
        self,
        target: Path,
        *,
        scenario_type: str,
        scenario_id: str,
        title: str,
        category: str,
        difficulty: str,
        description: str,
        dry_run: bool = False,
    ) -> AuthoringInitReport:
        """Create one fixed safe scaffold without overwriting author files."""
        if scenario_type not in {"baseline", "variant", "composite"}:
            issue = self._issue(
                "AUTHOR_SCENARIO_TYPE_INVALID",
                _relative_or_name(target, self._workspace),
                "Scenario type must be baseline, variant, or composite.",
            )
            return self._init_failure(scenario_type, target, dry_run, issue)

        if scenario_type == "variant":
            parent, parent_issue = self._resolve_target(target)
            if parent_issue is not None or parent is None:
                return self._init_failure(scenario_type, target, dry_run, parent_issue)
            if not (parent / "lab.yaml").is_file():
                issue = self._issue(
                    "AUTHOR_VARIANT_PARENT_INVALID",
                    self._display(parent),
                    "Variant scaffolds require an existing valid parent lab directory.",
                )
                return self._init_failure(scenario_type, parent, dry_run, issue)
            registry = LabRegistry(parent.parent, scanner=self._scanner)
            snapshot = registry.scan()
            loaded = next(
                (
                    lab
                    for lab in snapshot.labs
                    if (parent.parent / Path(lab.lab_path).parent).resolve() == parent
                ),
                None,
            )
            if loaded is None:
                issue = self._issue(
                    "AUTHOR_VARIANT_PARENT_INVALID",
                    self._display(parent / "lab.yaml"),
                    "The parent lab must pass Registry validation before adding a variant.",
                )
                return self._init_failure(scenario_type, parent, dry_run, issue)
            destination = parent / "variants" / scenario_id
            sequence = (
                max((item.definition.metadata.sequence for item in loaded.variants), default=0) + 1
            )
            try:
                files = variant_template(
                    variant_id=scenario_id,
                    sequence=sequence,
                    name=title,
                    description=description,
                    namespace=loaded.definition.environment.namespace,
                )
            except (ValidationError, ValueError):
                issue = self._issue(
                    "AUTHOR_INPUT_INVALID",
                    self._display(destination),
                    "The supplied variant identity or metadata is invalid.",
                )
                return self._init_failure(scenario_type, destination, dry_run, issue)
        else:
            new_destination, destination_issue = self._resolve_new_destination(target)
            if destination_issue is not None or new_destination is None:
                return self._init_failure(scenario_type, target, dry_run, destination_issue)
            destination = new_destination
            if destination.name != scenario_id:
                issue = self._issue(
                    "AUTHOR_DIRECTORY_ID_MISMATCH",
                    self._display(destination),
                    "A new lab directory name must exactly match its lab ID.",
                )
                return self._init_failure(scenario_type, destination, dry_run, issue)
            try:
                template = composite_template if scenario_type == "composite" else baseline_template
                files = template(
                    lab_id=scenario_id,
                    title=title,
                    category=category,
                    difficulty=difficulty,
                    description=description,
                )
            except (ValidationError, ValueError):
                issue = self._issue(
                    "AUTHOR_INPUT_INVALID",
                    self._display(destination),
                    "The supplied lab identity or metadata is invalid.",
                )
                return self._init_failure(scenario_type, destination, dry_run, issue)

        target_display = self._display(destination)
        existing_issue = self._destination_conflict(destination)
        if existing_issue is not None:
            return self._init_failure(scenario_type, destination, dry_run, existing_issue)
        relative_files = tuple(sorted(files))
        if dry_run:
            return AuthoringInitReport(
                passed=True,
                scenarioType=scenario_type,
                target=target_display,
                files=relative_files,
                dryRun=True,
            )
        try:
            self._write_scaffold(destination, files)
        except OSError:
            issue = self._issue(
                "AUTHOR_INIT_WRITE_FAILED",
                target_display,
                "The scaffold could not be written atomically; no target files were replaced.",
                exit_code=10,
            )
            return self._init_failure(scenario_type, destination, dry_run, issue)
        return AuthoringInitReport(
            passed=True,
            scenarioType=scenario_type,
            target=target_display,
            files=relative_files,
            dryRun=False,
        )

    def lint(self, target: Path) -> AuthoringLintReport:
        loaded = self._load_target(target)
        issues = list(loaded.issues)
        for scenario in loaded.scenarios:
            issues.extend(self._lint_scenario(loaded.registry, scenario))
        ordered = _sort_issues(issues)
        return AuthoringLintReport(
            passed=not any(issue.severity is IssueSeverity.ERROR for issue in ordered),
            scenarios=tuple(item.scenario_id for item in loaded.scenarios),
            issues=ordered,
            errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
            warningCount=sum(issue.severity is IssueSeverity.WARNING for issue in ordered),
        )

    def test(self, target: Path) -> AuthoringTestReport:
        loaded = self._load_target(target)
        lint_issues = list(loaded.issues)
        for scenario in loaded.scenarios:
            lint_issues.extend(self._lint_scenario(loaded.registry, scenario))
        if any(issue.severity is IssueSeverity.ERROR for issue in lint_issues):
            ordered = _sort_issues(lint_issues)
            return AuthoringTestReport(
                passed=False,
                results=(),
                issues=ordered,
                errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
            )

        results: list[ScenarioResult] = []
        issues: list[AuthoringIssue] = list(lint_issues)
        for scenario in loaded.scenarios:
            try:
                self._fake_runner.run(scenario.executable, scenario.contract)
            except FakeContractError as exc:
                issues.append(
                    self._issue(
                        exc.code,
                        self._display(scenario.directory / "authoring.yaml"),
                        exc.message,
                        field_path=exc.field_path,
                        exit_code=4,
                    )
                )
                results.append(
                    ScenarioResult(
                        scenario=scenario.scenario_id,
                        scenarioType=scenario.scenario_type,
                        passed=False,
                    )
                )
            else:
                results.append(
                    ScenarioResult(
                        scenario=scenario.scenario_id,
                        scenarioType=scenario.scenario_type,
                        passed=True,
                    )
                )
        ordered = _sort_issues(issues)
        return AuthoringTestReport(
            passed=all(item.passed for item in results)
            and not any(issue.severity is IssueSeverity.ERROR for issue in ordered),
            results=tuple(results),
            issues=ordered,
            errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
        )

    def test_integration(self, target: Path, *, junit: Path | None = None) -> AuthoringTestReport:
        """Fail closed before any optional local-cluster runner is constructed."""
        if os.environ.get("KUBELAB_RUN_LAB_INTEGRATION") != "1":
            issue = self._issue(
                "AUTHOR_INTEGRATION_DISABLED",
                ".",
                "Set KUBELAB_RUN_LAB_INTEGRATION=1 to enable local integration testing.",
                exit_code=5,
            )
        elif (
            platform.system() != "Linux"
            or "ubuntu" not in os.environ.get("WSL_DISTRO_NAME", "").casefold()
        ):
            issue = self._issue(
                "AUTHOR_INTEGRATION_PLATFORM_UNSUPPORTED",
                ".",
                "Author integration tests require WSL2 Ubuntu.",
                exit_code=5,
            )
        else:
            from kubelab.authoring_integration import run_author_integration

            return run_author_integration(self, target, junit=junit)
        return AuthoringTestReport(passed=False, results=(), issues=(issue,), errorCount=1)

    def inspect(self, target: Path) -> AuthoringInspectReport:
        loaded = self._load_target(target)
        issues = list(loaded.issues)
        inspections: list[ScenarioInspection] = []
        for scenario in loaded.scenarios:
            scenario_issues = self._lint_scenario(loaded.registry, scenario)
            issues.extend(scenario_issues)
            if any(item.severity is IssueSeverity.ERROR for item in scenario_issues):
                continue
            inspections.append(self._inspect_scenario(loaded.registry, scenario))
        ordered = _sort_issues(issues)
        return AuthoringInspectReport(
            passed=not any(issue.severity is IssueSeverity.ERROR for issue in ordered),
            scenarios=tuple(inspections),
            issues=ordered,
            errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
            warningCount=sum(issue.severity is IssueSeverity.WARNING for issue in ordered),
        )

    def package(self, target: Path, *, output: Path | None = None) -> AuthoringPackageReport:
        loaded = self._load_target(target)
        issues = list(loaded.issues)
        for scenario in loaded.scenarios:
            issues.extend(self._lint_scenario(loaded.registry, scenario))
        if loaded.target.family_directory is None or loaded.target.variant_directory is not None:
            issues.append(
                self._issue(
                    "AUTHOR_PACKAGE_TARGET_INVALID",
                    self._display(loaded.target.path),
                    "Packaging requires exactly one complete lab-family directory.",
                )
            )
        parents = {scenario.parent.definition.metadata.id for scenario in loaded.scenarios}
        if len(parents) != 1:
            issues.append(
                self._issue(
                    "AUTHOR_PACKAGE_TARGET_INVALID",
                    self._display(loaded.target.path),
                    "Packaging requires exactly one valid lab family.",
                )
            )
        if any(issue.severity is IssueSeverity.ERROR for issue in issues):
            ordered = _sort_issues(issues)
            return AuthoringPackageReport(
                passed=False,
                fileCount=0,
                issues=ordered,
            )
        test_report = self.test(target)
        if not test_report.passed:
            return AuthoringPackageReport(
                passed=False,
                fileCount=0,
                issues=test_report.issues,
            )
        assert loaded.target.family_directory is not None
        lab_id = next(iter(parents))
        requested_output = output or (self._workspace / f"{lab_id}.kubelab-lab.tar.gz")
        destination, output_issue = self._resolve_package_output(requested_output)
        if output_issue is not None or destination is None:
            return AuthoringPackageReport(
                passed=False,
                labId=lab_id,
                fileCount=0,
                issues=(output_issue,) if output_issue else (),
            )
        try:
            payload, file_count = self._build_package(
                loaded.target.family_directory,
                lab_id=lab_id,
                scenario_ids=tuple(item.scenario_id for item in loaded.scenarios),
            )
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(payload)
                temporary.replace(destination)
                self._verify_package(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except (OSError, ValueError, tarfile.TarError):
            issue = self._issue(
                "AUTHOR_PACKAGE_FAILED",
                self._display(destination),
                "The deterministic package could not be built or verified.",
                exit_code=10,
            )
            return AuthoringPackageReport(
                passed=False,
                labId=lab_id,
                fileCount=0,
                issues=(issue,),
            )
        return AuthoringPackageReport(
            passed=True,
            labId=lab_id,
            output=self._display(destination),
            sha256=hashlib.sha256(payload).hexdigest(),
            fileCount=file_count,
            issues=(),
        )

    def _load_target(self, target: Path) -> _LoadedTarget:
        resolved, path_issue = self._resolve_target(target)
        if path_issue is not None or resolved is None:
            fallback = _Target(self._workspace, self._workspace, None, None)
            return _LoadedTarget(
                fallback,
                LabRegistry(self._workspace),
                (),
                (path_issue,) if path_issue else (),
            )
        target_info = self._classify_target(resolved)
        registry = LabRegistry(target_info.catalog_root, scanner=self._scanner)
        snapshot = registry.scan()
        issues = [
            self._issue(
                error.code.value,
                error.lab_path,
                error.message,
                field_path=error.field_path,
                exit_code=(
                    3
                    if error.code.value.startswith("MANIFEST_")
                    or error.code.value == "LAB_PATH_ESCAPE"
                    else 2
                ),
            )
            for error in snapshot.errors
            if self._registry_error_in_target(error.lab_path, target_info)
        ]
        scenarios: list[_Scenario] = []
        for loaded in snapshot.labs:
            family = (target_info.catalog_root / Path(loaded.lab_path).parent).resolve()
            if target_info.family_directory is not None and family != target_info.family_directory:
                continue
            if target_info.variant_directory is None:
                baseline = self._load_scenario(
                    registry,
                    parent=loaded,
                    executable=loaded,
                    variant=None,
                    directory=family,
                )
                if isinstance(baseline, AuthoringIssue):
                    issues.append(baseline)
                else:
                    scenarios.append(baseline)
            for variant in loaded.variants:
                variant_dir = (
                    target_info.catalog_root / Path(variant.variant_path).parent
                ).resolve()
                if (
                    target_info.variant_directory is not None
                    and variant_dir != target_info.variant_directory
                ):
                    continue
                effective = registry.resolve_variant(loaded, variant.definition.metadata.id)
                if not isinstance(effective, EffectiveLab):
                    continue
                scenario = self._load_scenario(
                    registry,
                    parent=loaded,
                    executable=effective,
                    variant=variant,
                    directory=variant_dir,
                )
                if isinstance(scenario, AuthoringIssue):
                    issues.append(scenario)
                else:
                    scenarios.append(scenario)
        if not scenarios and not issues:
            issues.append(
                self._issue(
                    "AUTHOR_TARGET_EMPTY",
                    self._display(resolved),
                    "The target does not contain a loadable KubeLab scenario.",
                )
            )
        return _LoadedTarget(target_info, registry, tuple(scenarios), tuple(issues))

    def _resolve_target(self, target: Path) -> tuple[Path | None, AuthoringIssue | None]:
        candidate = target if target.is_absolute() else self._workspace / target
        try:
            unresolved = candidate.absolute()
            if any(part.is_symlink() for part in _parents_to(unresolved, self._workspace)):
                return None, self._issue(
                    "AUTHOR_PATH_SYMLINK",
                    _relative_or_name(unresolved, self._workspace),
                    "Author targets must not traverse symbolic links.",
                    exit_code=3,
                )
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None, self._issue(
                "AUTHOR_TARGET_INVALID",
                _relative_or_name(candidate, self._workspace),
                "The author target does not exist or cannot be read.",
            )
        if not _is_within(resolved, self._workspace) or not resolved.is_dir():
            return None, self._issue(
                "AUTHOR_PATH_ESCAPE",
                _relative_or_name(candidate, self._workspace),
                "The author target must be a directory inside the current workspace.",
                exit_code=3,
            )
        return resolved, None

    def _resolve_new_destination(self, target: Path) -> tuple[Path | None, AuthoringIssue | None]:
        candidate = target if target.is_absolute() else self._workspace / target
        lexical_parent = candidate.absolute().parent
        if any(part.is_symlink() for part in _parents_to(lexical_parent, self._workspace)):
            return None, self._issue(
                "AUTHOR_PATH_SYMLINK",
                _relative_or_name(candidate, self._workspace),
                "Scaffold targets must not traverse symbolic links.",
                exit_code=3,
            )
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError:
            return None, self._issue(
                "AUTHOR_TARGET_INVALID",
                _relative_or_name(candidate, self._workspace),
                "The target parent directory must already exist.",
            )
        destination = parent / candidate.name
        if not _is_within(destination, self._workspace):
            return None, self._issue(
                "AUTHOR_PATH_ESCAPE",
                candidate.name,
                "The scaffold target must stay inside the current workspace.",
                exit_code=3,
            )
        return destination, None

    def _destination_conflict(self, destination: Path) -> AuthoringIssue | None:
        if not destination.exists():
            return None
        if destination.is_symlink() or not destination.is_dir():
            return self._issue(
                "AUTHOR_TARGET_CONFLICT",
                self._display(destination),
                "The scaffold target already exists and is not an empty directory.",
            )
        try:
            nonempty = next(destination.iterdir(), None) is not None
        except OSError:
            nonempty = True
        if nonempty:
            return self._issue(
                "AUTHOR_TARGET_CONFLICT",
                self._display(destination),
                "The scaffold target directory is not empty.",
            )
        return None

    def _write_scaffold(self, destination: Path, files: Mapping[str, bytes]) -> None:
        parent = destination.parent
        parent_created = False
        if not parent.exists():
            parent.mkdir()
            parent_created = True
        staging = parent / f"kubelab-init-{uuid4().hex}.tmp"
        if staging.exists():  # pragma: no cover - UUID collision
            raise OSError("staging collision")
        staging.mkdir()
        try:
            for relative, content in sorted(files.items()):
                path = staging.joinpath(*PurePosixPath(relative).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(content.replace(b"\r\n", b"\n"))
            if destination.exists():
                destination.rmdir()
            staging.replace(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if parent_created and parent.exists() and next(parent.iterdir(), None) is None:
                parent.rmdir()

    def _init_failure(
        self,
        scenario_type: str,
        target: Path,
        dry_run: bool,
        issue: AuthoringIssue | None,
    ) -> AuthoringInitReport:
        actual = issue or self._issue(
            "AUTHOR_INTERNAL_ERROR",
            _relative_or_name(target, self._workspace),
            "The scaffold request could not be completed.",
            exit_code=10,
        )
        return AuthoringInitReport(
            passed=False,
            scenarioType=scenario_type,
            target=actual.relative_path,
            files=(),
            dryRun=dry_run,
            issues=(actual,),
        )

    def _inspect_scenario(self, registry: LabRegistry, scenario: _Scenario) -> ScenarioInspection:
        documents = registry.materialize_for_gateway(scenario.executable).documents
        resources = tuple(
            sorted(
                (
                    AuthoringResourceSummary(
                        apiVersion=str(document.data.get("apiVersion", "")),
                        kind=str(document.data.get("kind", "")),
                        name=str(
                            document.data.get("metadata", {}).get("name", "")
                            if isinstance(document.data.get("metadata"), Mapping)
                            else ""
                        ),
                    )
                    for document in documents
                ),
                key=lambda item: (item.kind, item.name),
            )
        )
        images = tuple(
            sorted({image for document in documents for image in _manifest_images(document.data)})
        )
        repair_items = [("full", scenario.contract.repairs.full)]
        if scenario.contract.repairs.first is not None:
            repair_items.append(("first", scenario.contract.repairs.first))
        repairs = tuple(
            RepairSummary(
                stage=stage,
                manifest=plan.manifest,
                changes=tuple(
                    sorted(
                        f"{change.resource.kind}/{change.resource.name}:{path}"
                        for change in plan.allowed_changes
                        for path in (change.paths or ("<create>",))
                    )
                ),
            )
            for stage, plan in repair_items
        )
        definition = scenario.executable.definition
        before, after = _disclosure_previews(scenario)
        file_paths = self._scenario_files(scenario)
        hashes = {
            relative: hashlib.sha256(path.read_bytes()).hexdigest() for relative, path in file_paths
        }
        return ScenarioInspection(
            scenario=scenario.scenario_id,
            scenarioType=scenario.scenario_type,
            inheritedFrom=(scenario.parent.definition.metadata.id if scenario.variant else None),
            resources=resources,
            images=images,
            initialCheckTypes=tuple(check.type for check in definition.initial_checks),
            successCheckTypes=tuple(check.type for check in definition.success_checks),
            repairs=repairs,
            beforePass=before,
            afterPass=after,
            files=tuple(relative for relative, _ in file_paths),
            fileSha256=hashes,
        )

    def _scenario_files(self, scenario: _Scenario) -> tuple[tuple[str, Path], ...]:
        files: list[tuple[str, Path]] = []
        for path in sorted(scenario.directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(scenario.directory)
            if scenario.variant is None and relative.parts and relative.parts[0] == "variants":
                continue
            files.append((relative.as_posix(), path))
        return tuple(files)

    def _resolve_package_output(self, output: Path) -> tuple[Path | None, AuthoringIssue | None]:
        candidate = output if output.is_absolute() else self._workspace / output
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError:
            return None, self._issue(
                "AUTHOR_PACKAGE_OUTPUT_INVALID",
                _relative_or_name(candidate, self._workspace),
                "The package output directory must already exist.",
            )
        resolved = parent / candidate.name
        if not _is_within(resolved, self._workspace) or any(
            part.is_symlink() for part in _parents_to(parent, self._workspace)
        ):
            return None, self._issue(
                "AUTHOR_PATH_ESCAPE",
                _relative_or_name(candidate, self._workspace),
                "The package output must stay inside the author workspace.",
                exit_code=3,
            )
        if resolved.exists():
            return None, self._issue(
                "AUTHOR_PACKAGE_OUTPUT_EXISTS",
                self._display(resolved),
                "The package output already exists; author tools never overwrite files.",
            )
        if not resolved.name.endswith(".kubelab-lab.tar.gz"):
            return None, self._issue(
                "AUTHOR_PACKAGE_OUTPUT_INVALID",
                self._display(resolved),
                "Package output must end with .kubelab-lab.tar.gz.",
            )
        return resolved, None

    def _build_package(
        self,
        family: Path,
        *,
        lab_id: str,
        scenario_ids: tuple[str, ...],
    ) -> tuple[bytes, int]:
        entries: dict[str, bytes] = {}
        total_bytes = 0
        for path in sorted(family.rglob("*")):
            relative = path.relative_to(family)
            if path.is_symlink():
                raise ValueError("symlinks are forbidden")
            if path.is_dir():
                continue
            if _package_file_forbidden(relative):
                raise ValueError("forbidden package file")
            if path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise ValueError("package file exceeds size limit")
            content = path.read_bytes()
            if path.suffix.casefold() in {".yaml", ".yml", ".json", ".md", ".txt"}:
                content = content.replace(b"\r\n", b"\n")
            if _package_sensitive(content):
                raise ValueError("package contains sensitive content")
            total_bytes += len(content)
            if total_bytes > _MAX_PACKAGE_BYTES or len(entries) >= _MAX_PACKAGE_FILES:
                raise ValueError("package exceeds bounded content limits")
            archive_path = f"labs/{family.name}/{relative.as_posix()}"
            entries[archive_path] = content
        file_index = [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(entries.items())
        ]
        index = {
            "formatVersion": 1,
            "labId": lab_id,
            "schemaVersions": {
                "lab": "kubelab.io/v1alpha1",
                "authoring": "kubelab.io/v1alpha1",
            },
            "scenarios": list(scenario_ids),
            "files": file_index,
        }
        entries["index.json"] = (
            json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(entries.items()):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(content))
        output = io.BytesIO()
        with gzip.GzipFile(
            filename="", fileobj=output, mode="wb", compresslevel=9, mtime=0
        ) as stream:
            stream.write(tar_buffer.getvalue())
        return output.getvalue(), len(file_index)

    @staticmethod
    def _verify_package(path: Path) -> None:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > _MAX_PACKAGE_FILES + 1 or sum(member.size for member in members) > (
                _MAX_PACKAGE_BYTES + _MAX_MANIFEST_BYTES
            ):
                raise ValueError("archive exceeds bounded content limits")
            if any(
                not member.isfile()
                or member.issym()
                or member.islnk()
                or PurePosixPath(member.name).is_absolute()
                or ".." in PurePosixPath(member.name).parts
                for member in members
            ):
                raise ValueError("unsafe archive member")
            index_member = archive.getmember("index.json")
            index_stream = archive.extractfile(index_member)
            if index_stream is None:
                raise ValueError("missing package index")
            index = json.loads(index_stream.read())
            expected = {item["path"]: item for item in index["files"]}
            actual = {member.name: member for member in members if member.name != "index.json"}
            if set(actual) != set(expected):
                raise ValueError("package index mismatch")
            for name, member in actual.items():
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("package member cannot be read")
                content = stream.read()
                if len(content) != expected[name]["size"] or (
                    hashlib.sha256(content).hexdigest() != expected[name]["sha256"]
                ):
                    raise ValueError("package digest mismatch")

    @staticmethod
    def _classify_target(path: Path) -> _Target:
        if (path / "variant.yaml").is_file() and path.parent.name == "variants":
            family = path.parent.parent.resolve()
            return _Target(path, family.parent, family, path)
        if (path / "lab.yaml").is_file():
            return _Target(path, path.parent, path, None)
        return _Target(path, path, None, None)

    @staticmethod
    def _registry_error_in_target(error_path: str, target: _Target) -> bool:
        if target.family_directory is None:
            return True
        family_name = target.family_directory.name
        return error_path == family_name or error_path.startswith(f"{family_name}/")

    def _load_scenario(
        self,
        registry: LabRegistry,
        *,
        parent: LoadedLab,
        executable: ExecutableLab,
        variant: LoadedVariant | None,
        directory: Path,
    ) -> _Scenario | AuthoringIssue:
        del registry
        contract_path = directory / "authoring.yaml"
        relative = self._display(contract_path)
        try:
            resolved = contract_path.resolve(strict=True)
        except OSError:
            return self._issue(
                "AUTHOR_CONTRACT_MISSING",
                relative,
                "Every baseline and fixed variant requires authoring.yaml.",
            )
        if (
            contract_path.is_symlink()
            or not resolved.is_file()
            or not _is_within(resolved, directory.resolve())
        ):
            return self._issue(
                "AUTHOR_PATH_ESCAPE",
                relative,
                "authoring.yaml must be a regular file inside its scenario directory.",
                exit_code=3,
            )
        try:
            if resolved.stat().st_size > _MAX_AUTHORING_BYTES:
                raise ValueError("authoring.yaml exceeds the 256 KiB limit")
            documents = load_all_unique(resolved.read_text(encoding="utf-8"))
            if len(documents) != 1 or not isinstance(documents[0], Mapping):
                raise ValueError("authoring.yaml must contain exactly one mapping")
            contract = LabAuthoringContract.model_validate(documents[0])
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            field_path = None
            if isinstance(exc, ValidationError) and exc.errors(include_input=False):
                field_path = ".".join(str(part) for part in exc.errors()[0]["loc"])
            return self._issue(
                "AUTHOR_CONTRACT_INVALID",
                relative,
                "The author contract is not valid UTF-8 v1alpha1 YAML.",
                field_path=field_path,
            )
        scenario_id = (
            parent.definition.metadata.id
            if variant is None
            else f"{parent.definition.metadata.id}/{variant.definition.metadata.id}"
        )
        return _Scenario(
            scenario_id=scenario_id,
            scenario_type=contract.scenario_type,
            directory=directory,
            parent=parent,
            executable=executable,
            variant=variant,
            contract=contract,
        )

    def _lint_scenario(
        self, registry: LabRegistry, scenario: _Scenario
    ) -> tuple[AuthoringIssue, ...]:
        issues: list[AuthoringIssue] = []
        contract_file = self._display(scenario.directory / "authoring.yaml")
        expected_type = "variant" if scenario.variant is not None else scenario.scenario_type
        if scenario.variant is not None and scenario.scenario_type != "variant":
            issues.append(
                self._issue(
                    "AUTHOR_SCENARIO_TYPE_MISMATCH",
                    contract_file,
                    "A variant directory must declare scenarioType variant.",
                    field_path="scenarioType",
                )
            )
        if scenario.variant is None and expected_type == "variant":
            issues.append(
                self._issue(
                    "AUTHOR_SCENARIO_TYPE_MISMATCH",
                    contract_file,
                    "A baseline directory cannot declare scenarioType variant.",
                    field_path="scenarioType",
                )
            )

        definition = scenario.executable.definition
        if len(definition.hints) != 3:
            issues.append(
                self._issue(
                    "AUTHOR_HINT_COUNT",
                    self._definition_path(scenario),
                    "Author-ready scenarios require exactly three hint levels.",
                    field_path="hints",
                )
            )
        elif not _safe_workspace_command(definition.hints[1].content):
            issues.append(
                self._issue(
                    "AUTHOR_HINT_COMMAND_UNSAFE",
                    self._definition_path(scenario),
                    "The second hint must be one fixed kubectl command without shell operators.",
                    field_path="hints.1.content",
                    exit_code=3,
                )
            )
        if len(scenario.parent.definition.interview.questions) != 3:
            issues.append(
                self._issue(
                    "AUTHOR_REVIEW_QUESTION_COUNT",
                    self._display(
                        (
                            scenario.directory
                            if scenario.variant is None
                            else scenario.directory.parents[1]
                        )
                        / "lab.yaml"
                    ),
                    "Author-ready lab families require exactly three retrospective questions.",
                    field_path="interview.questions",
                )
            )
        readme = scenario.directory / "README.md"
        if not readme.is_file() or readme.is_symlink():
            issues.append(
                self._issue(
                    "AUTHOR_README_MISSING",
                    self._display(readme),
                    "Every scenario requires a regular UTF-8 README.md.",
                )
            )

        checks = tuple((*definition.initial_checks, *definition.success_checks))
        states = [
            ("states.faulted", scenario.contract.states.faulted),
            ("states.repaired", scenario.contract.states.repaired),
        ]
        if scenario.contract.states.first_repair is not None:
            states.append(("states.firstRepair", scenario.contract.states.first_repair))
        from kubelab.authoring_fake import DeclarativeFakeGateway

        for field_path, state in states:
            try:
                DeclarativeFakeGateway(checks, state)
            except FakeContractError as exc:
                suffix = exc.field_path.removeprefix("observations.") if exc.field_path else None
                full_path = f"{field_path}.observations"
                if suffix:
                    full_path = f"{full_path}.{suffix}"
                issues.append(
                    self._issue(
                        exc.code,
                        contract_file,
                        exc.message,
                        field_path=full_path,
                        exit_code=4,
                    )
                )

        try:
            initial_documents = registry.materialize_for_gateway(scenario.executable).documents
        except Exception:
            issues.append(
                self._issue(
                    "AUTHOR_SOURCE_CHANGED",
                    self._definition_path(scenario),
                    "The scenario source changed after Registry validation.",
                    exit_code=3,
                )
            )
            return tuple(issues)

        repair_items = [("repairs.full", scenario.contract.repairs.full)]
        if scenario.contract.repairs.first is not None:
            repair_items.append(("repairs.first", scenario.contract.repairs.first))
        for field_path, plan in repair_items:
            issues.extend(
                self._lint_repair(
                    scenario,
                    initial_documents,
                    plan,
                    field_path=field_path,
                )
            )
        issues.extend(self._lint_public_content(scenario))
        return tuple(issues)

    @property
    def _docs_anchor(self) -> str:
        return "docs/M8_AUTHORING_CONTRACT.md"

    def _lint_repair(
        self,
        scenario: _Scenario,
        initial_documents: tuple[ManifestDocument, ...],
        plan: RepairPlan,
        *,
        field_path: str,
    ) -> tuple[AuthoringIssue, ...]:
        issues: list[AuthoringIssue] = []
        repair_path, path_issue = self._resolve_scenario_file(scenario, plan.manifest)
        if path_issue is not None or repair_path is None:
            return (path_issue,) if path_issue else ()
        documents, load_issue = self._load_manifest_documents(repair_path)
        if load_issue is not None:
            return (load_issue,)
        namespace = scenario.parent.definition.environment.namespace
        scan_issues = self._scanner.scan(documents, namespace=namespace)
        issues.extend(
            self._issue(
                issue.code,
                issue.manifest_path,
                issue.message,
                field_path=issue.field_path,
                exit_code=3,
            )
            for issue in scan_issues
        )
        for document in documents:
            if document.data.get("kind") == "Secret":
                issues.append(
                    self._issue(
                        "AUTHOR_SECRET_FORBIDDEN",
                        document.manifest_path,
                        "Author packages must not contain Kubernetes Secret resources.",
                        field_path=f"documents[{document.document_index}].kind",
                        exit_code=3,
                    )
                )
        if issues:
            return tuple(issues)

        initial = {_resource_identity(item.data): item.data for item in initial_documents}
        repaired = {_resource_identity(item.data): item.data for item in documents}
        allowed = {
            (
                item.resource.api_version,
                item.resource.kind,
                item.resource.name,
            ): item
            for item in plan.allowed_changes
        }
        if None in initial or None in repaired:
            return (
                self._issue(
                    "AUTHOR_REPAIR_RESOURCE_INVALID",
                    self._display(repair_path),
                    "Every repair document requires apiVersion, kind, and metadata.name.",
                    field_path=field_path,
                ),
            )
        for identity, repaired_document in repaired.items():
            assert identity is not None
            change = allowed.get(identity)
            if change is None:
                issues.append(
                    self._issue(
                        "AUTHOR_REPAIR_RESOURCE_UNDECLARED",
                        self._display(repair_path),
                        "The repair contains a resource missing from allowedChanges.",
                        field_path=f"{field_path}.allowedChanges",
                    )
                )
                continue
            original = initial.get(identity)
            if change.operation == "create":
                if original is not None:
                    issues.append(
                        self._issue(
                            "AUTHOR_REPAIR_CREATE_EXISTS",
                            self._display(repair_path),
                            (
                                "A create repair must target a resource absent from the "
                                "fault Manifest."
                            ),
                            field_path=f"{field_path}.allowedChanges",
                        )
                    )
                continue
            if original is None:
                issues.append(
                    self._issue(
                        "AUTHOR_REPAIR_TARGET_MISSING",
                        self._display(repair_path),
                        "A modify or recreate repair must target an existing resource.",
                        field_path=f"{field_path}.allowedChanges",
                    )
                )
                continue
            if change.operation == "recreate" and identity[1] not in _RECREATE_KINDS:
                issues.append(
                    self._issue(
                        "AUTHOR_RECREATE_KIND_FORBIDDEN",
                        self._display(repair_path),
                        "Only fixed replacement-required resource kinds may use recreate.",
                        field_path=f"{field_path}.allowedChanges",
                        exit_code=3,
                    )
                )
            differences = _diff_paths(original, repaired_document)
            undeclared = [
                path
                for path in differences
                if not any(_pointer_within(path, allowed_path) for allowed_path in change.paths)
            ]
            if undeclared:
                issues.append(
                    self._issue(
                        "AUTHOR_REPAIR_DIFF_UNDECLARED",
                        self._display(repair_path),
                        "The repair changes a field outside its declared JSON Pointer boundary.",
                        field_path=undeclared[0],
                    )
                )
        unused = set(allowed) - set(repaired)
        if unused:
            issues.append(
                self._issue(
                    "AUTHOR_REPAIR_DECLARATION_UNUSED",
                    self._display(repair_path),
                    "Every allowedChanges resource must appear in the repair Manifest.",
                    field_path=f"{field_path}.allowedChanges",
                )
            )
        return tuple(issues)

    def _resolve_scenario_file(
        self, scenario: _Scenario, relative: str
    ) -> tuple[Path | None, AuthoringIssue | None]:
        reference = PurePosixPath(relative)
        candidate = scenario.directory.joinpath(*reference.parts)
        display = self._display(candidate)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None, self._issue(
                "AUTHOR_REPAIR_MISSING", display, "The declared repair Manifest is missing."
            )
        if (
            candidate.is_symlink()
            or not resolved.is_file()
            or not _is_within(resolved, scenario.directory.resolve())
        ):
            return None, self._issue(
                "AUTHOR_PATH_ESCAPE",
                display,
                "Repair files must be regular files inside the scenario directory.",
                exit_code=3,
            )
        return resolved, None

    def _load_manifest_documents(
        self, path: Path
    ) -> tuple[tuple[ManifestDocument, ...], AuthoringIssue | None]:
        display = self._display(path)
        try:
            if path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise ValueError("Manifest exceeds size limit")
            values = load_all_unique(path.read_text(encoding="utf-8"))
            if not values or len(values) > _MAX_MANIFEST_DOCUMENTS:
                raise ValueError("Manifest document count is invalid")
            if any(not isinstance(value, Mapping) for value in values):
                raise ValueError("Manifest documents must be mappings")
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            return (), self._issue(
                "AUTHOR_REPAIR_YAML_INVALID",
                display,
                "The repair must contain bounded UTF-8 YAML mapping documents.",
            )
        return (
            tuple(
                ManifestDocument(
                    manifest_path=display,
                    document_index=index,
                    data=value,
                )
                for index, value in enumerate(values)
            ),
            None,
        )

    def _lint_public_content(self, scenario: _Scenario) -> tuple[AuthoringIssue, ...]:
        definition = scenario.executable.definition
        values = [
            definition.metadata.name,
            definition.metadata.description,
            definition.task.description,
            definition.task.completion_description,
            definition.task.success_message,
            *(hint.content for hint in definition.hints),
            *scenario.parent.definition.interview.questions,
        ]
        if scenario.variant is not None:
            variant = scenario.variant.definition
            values.extend(
                (
                    variant.metadata.name,
                    variant.metadata.description,
                    variant.reveal.key_evidence,
                    variant.reveal.root_cause,
                    variant.reveal.resolution,
                    variant.reveal.prevention,
                )
            )
        readme = scenario.directory / "README.md"
        try:
            if readme.is_file() and readme.stat().st_size <= _MAX_AUTHORING_BYTES:
                values.append(readme.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return (
                self._issue(
                    "AUTHOR_README_INVALID",
                    self._display(readme),
                    "README.md must be bounded UTF-8 text.",
                ),
            )
        text = "\n".join(values)
        issue = _public_leak(text)
        if issue is None:
            return ()
        code, message = issue
        return (
            self._issue(
                code,
                self._definition_path(scenario),
                message,
                exit_code=3,
            ),
        )

    def _definition_path(self, scenario: _Scenario) -> str:
        name = "variant.yaml" if scenario.variant is not None else "lab.yaml"
        return self._display(scenario.directory / name)

    def _display(self, path: Path) -> str:
        return _relative_or_name(path, self._workspace)

    def _issue(
        self,
        code: str,
        relative_path: str,
        message: str,
        *,
        field_path: str | None = None,
        severity: IssueSeverity = IssueSeverity.ERROR,
        exit_code: int = 2,
    ) -> AuthoringIssue:
        return AuthoringIssue(
            code=code,
            severity=severity,
            relativePath=relative_path.replace("\\", "/"),
            fieldPath=field_path,
            message=message,
            docsAnchor=self._docs_anchor,
            exit_code=exit_code,
        )


def _disclosure_previews(scenario: _Scenario) -> tuple[DisclosurePreview, DisclosurePreview]:
    definition = scenario.executable.definition
    all_check_types = tuple(
        check.type for check in (*definition.initial_checks, *definition.success_checks)
    )
    if scenario.variant is None:
        preview = DisclosurePreview(
            revealed=True,
            task=definition.task.description,
            completionDescription=definition.task.completion_description,
            checkTypes=all_check_types,
        )
        return preview, preview
    before_disclosure = project_variant_disclosure(scenario.variant, revealed=False)
    after_disclosure = project_variant_disclosure(scenario.variant, revealed=True)
    before = DisclosurePreview(
        revealed=before_disclosure.revealed,
        task=definition.task.description,
        completionDescription=definition.task.completion_description,
        checkTypes=(),
    )
    after = DisclosurePreview(
        revealed=after_disclosure.revealed,
        task=definition.task.description,
        completionDescription=definition.task.completion_description,
        checkTypes=all_check_types,
        scenarioName=after_disclosure.scenario_name,
        scenarioDescription=after_disclosure.scenario_description,
        keyEvidence=after_disclosure.key_evidence,
        rootCause=after_disclosure.root_cause,
        resolution=after_disclosure.resolution,
        prevention=after_disclosure.prevention,
    )
    return before, after


def _manifest_images(value: Any) -> tuple[str, ...]:
    images: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "image" and isinstance(child, str):
                images.append(child)
            else:
                images.extend(_manifest_images(child))
    elif isinstance(value, list):
        for child in value:
            images.extend(_manifest_images(child))
    return tuple(images)


def _package_file_forbidden(path: Path) -> bool:
    lowered = tuple(part.casefold() for part in path.parts)
    if any(
        part
        in {
            ".cache",
            ".git",
            ".mypy_cache",
            ".nox",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        }
        or part.startswith((".git", ".venv"))
        for part in lowered
    ):
        return True
    name = path.name.casefold()
    return (
        name in {".coverage", ".env", ".ds_store", "kubeconfig"}
        or name.endswith((".db-shm", ".db-wal"))
        or path.suffix.casefold() in {".crt", ".db", ".key", ".log", ".pem", ".pyc", ".pyo"}
    )


def _package_sensitive(content: bytes) -> bool:
    patterns = (
        rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
        rb"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}",
        rb"(?im)^\s*(?:token|password|client-key-data|client-certificate-data)\s*:\s*\S+\s*$",
        rb"(?i)[A-Z]:\\Users\\[^\r\n]+",
        rb"(?i)/home/[^/\r\n]+/",
    )
    return any(re.search(pattern, content) for pattern in patterns)


def _resource_identity(value: Mapping[str, Any]) -> tuple[str, str, str] | None:
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    api_version = value.get("apiVersion")
    kind = value.get("kind")
    name = metadata.get("name")
    if not all(isinstance(item, str) and item for item in (api_version, kind, name)):
        return None
    assert isinstance(api_version, str) and isinstance(kind, str) and isinstance(name, str)
    return api_version, kind, name


def _diff_paths(left: Any, right: Any, prefix: str = "") -> tuple[str, ...]:
    if type(left) is not type(right):
        return (prefix or "/",)
    if isinstance(left, Mapping):
        paths: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{prefix}/{_escape_pointer(str(key))}"
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_diff_paths(left[key], right[key], child))
        return tuple(paths)
    if isinstance(left, list):
        if left == right:
            return ()
        return (prefix or "/",)
    if left != right:
        return (prefix or "/",)
    return ()


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer_within(path: str, allowed: str) -> bool:
    return path == allowed or path.startswith(f"{allowed}/")


def _safe_workspace_command(value: str) -> bool:
    stripped = value.strip()
    commands = tuple(part.strip() for part in stripped.split("&&"))
    return (
        bool(commands)
        and all(command.startswith("kubectl ") for command in commands)
        and not any(token in stripped for token in _SHELL_TOKENS)
    )


def _public_leak(value: str) -> tuple[str, str] | None:
    patterns = (
        (
            "AUTHOR_PUBLIC_CREDENTIAL",
            re.compile(
                r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{6,}|(?:token|password|authorization|private[_-]?key)\s*[:=]\s*\S+)"
            ),
            "Public learning content appears to contain credential material.",
        ),
        (
            "AUTHOR_PUBLIC_PRIVATE_KEY",
            re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
            "Public learning content must not contain private-key material.",
        ),
        (
            "AUTHOR_PUBLIC_STACK",
            re.compile(r"(?i)Traceback \(most recent call last\)|\bat\s+\S+\.py:\d+"),
            "Public learning content must not contain an exception stack.",
        ),
        (
            "AUTHOR_PUBLIC_LOCAL_PATH",
            re.compile(r"(?i)(?:[A-Z]:\\Users\\[^\s]+|/home/[^/\s]+/)"),
            "Public learning content must not contain a machine-local user path.",
        ),
        (
            "AUTHOR_PUBLIC_ACTIVE_HTML",
            re.compile(r"(?i)<\s*(?:script|iframe)|javascript:|\bonerror\s*="),
            "Public learning content must not contain active HTML or script content.",
        ),
    )
    for code, pattern, message in patterns:
        if pattern.search(value):
            return code, message
    if re.search(r"(?m)^\s*apiVersion\s*:", value) and re.search(r"(?m)^\s*kind\s*:", value):
        return (
            "AUTHOR_PUBLIC_MANIFEST",
            "Public learning content must not embed a complete Kubernetes Manifest.",
        )
    return None


def _parents_to(path: Path, workspace: Path) -> tuple[Path, ...]:
    values: list[Path] = []
    current = path
    while True:
        values.append(current)
        if current == workspace or current.parent == current:
            break
        current = current.parent
    return tuple(values)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root).as_posix()
    except ValueError:
        return path.name or "."


def _sort_issues(issues: Iterable[AuthoringIssue]) -> tuple[AuthoringIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.relative_path.casefold(),
                issue.field_path or "",
                issue.code,
            ),
        )
    )


def _issues_exit_code(issues: Iterable[AuthoringIssue]) -> int:
    errors = [issue for issue in issues if issue.severity is IssueSeverity.ERROR]
    if not errors:
        return 0
    codes = {issue.exit_code for issue in errors}
    for value in (10, 5, 4, 3, 2):
        if value in codes:
            return value
    return 10


__all__ = [
    "AuthoringInitReport",
    "AuthoringInspectReport",
    "AuthoringIssue",
    "AuthoringLintReport",
    "AuthoringPackageReport",
    "AuthoringService",
    "AuthoringTestReport",
    "IssueSeverity",
]
