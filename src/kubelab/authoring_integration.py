"""Explicitly gated local-minikube acceptance for M8 author scenarios."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from xml.etree import ElementTree

import yaml

from kubelab.config import ToolName, TrustedContext, load_config, resolve_kubeconfig_path
from kubelab.context_trust import build_context_trust_service, trusted_context_fingerprint
from kubelab.database import Database
from kubelab.doctor import build_doctor_service
from kubelab.guided_learning import EnvironmentReadinessService
from kubelab.kubernetes_gateway import KubernetesGateway, SessionScope
from kubelab.lab_manager import LabManager
from kubelab.lab_registry import LabRegistry
from kubelab.manifest_security import ManifestDocument
from kubelab.operation_lock import OperationLock
from kubelab.session_state import LabSessionSnapshot, ValidationStatus
from kubelab.tools import ProcessRunner, ToolExecutionError, ToolLocator
from kubelab.validation_engine import ValidationEngine

if TYPE_CHECKING:
    from kubelab.authoring import (
        AuthoringIssue,
        AuthoringService,
        AuthoringTestReport,
        ScenarioResult,
        _Scenario,
    )

_REQUIRED_IMAGES = frozenset(
    {
        "nginx:1.26-alpine",
        "nginx:1.27-alpine",
        "busybox:1.36.1",
        "curlimages/curl:8.12.1",
    }
)


def run_author_integration(
    service: AuthoringService,
    target: Path,
    *,
    junit: Path | None,
) -> AuthoringTestReport:
    """Run selected scenarios only after every read-only gate has passed."""
    from kubelab.authoring import AuthoringTestReport, IssueSeverity, ScenarioResult, _sort_issues

    loaded = service._load_target(target)
    issues: list[AuthoringIssue] = list(loaded.issues)
    for scenario in loaded.scenarios:
        issues.extend(service._lint_scenario(loaded.registry, scenario))
    if any(issue.severity is IssueSeverity.ERROR for issue in issues):
        ordered = _sort_issues(issues)
        return AuthoringTestReport(
            passed=False,
            results=(),
            issues=ordered,
            errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
        )

    prerequisite = _integration_prerequisite(service)
    if prerequisite is not None:
        return AuthoringTestReport(
            passed=False,
            results=(),
            issues=(prerequisite,),
            errorCount=1,
        )

    results: list[ScenarioResult] = []
    for scenario in loaded.scenarios:
        try:
            _run_scenario(service, loaded.registry, scenario)
        except Exception:
            issue = service._issue(
                "AUTHOR_INTEGRATION_CONTRACT_FAILED",
                service._display(scenario.directory / "authoring.yaml"),
                (
                    "The local integration lifecycle failed; inspect the owned Namespace "
                    "before retrying."
                ),
                exit_code=4,
            )
            issues.append(issue)
            results.append(
                ScenarioResult(
                    scenario=scenario.scenario_id,
                    scenarioType=scenario.scenario_type,
                    passed=False,
                )
            )
            break
        results.append(
            ScenarioResult(
                scenario=scenario.scenario_id,
                scenarioType=scenario.scenario_type,
                passed=True,
            )
        )
    ordered = _sort_issues(issues)
    report = AuthoringTestReport(
        passed=len(results) == len(loaded.scenarios)
        and all(item.passed for item in results)
        and not any(issue.severity is IssueSeverity.ERROR for issue in ordered),
        results=tuple(results),
        issues=ordered,
        errorCount=sum(issue.severity is IssueSeverity.ERROR for issue in ordered),
    )
    if junit is not None:
        junit_issue = _write_junit(service, junit, report)
        if junit_issue is not None:
            return AuthoringTestReport(
                passed=False,
                results=report.results,
                issues=(*report.issues, junit_issue),
                errorCount=report.error_count + 1,
            )
    return report


def _integration_prerequisite(service: AuthoringService) -> AuthoringIssue | None:
    try:
        config = load_config()
        trust = build_context_trust_service()
        record = trust.assert_trusted_context()
        tool = ToolLocator(config.tools).locate(ToolName.MINIKUBE)
        if tool is None:
            raise ValueError("minikube unavailable")
        runner = ProcessRunner()
        profiles = runner.run(tool.path, ["profile", "list", "--output=json"])
        if profiles.returncode != 0 or profiles.truncated:
            raise ValueError("minikube profiles unavailable")
        payload = json.loads(profiles.stdout)
        if not _docker_profile(payload, record.minikube_profile):
            return service._issue(
                "AUTHOR_INTEGRATION_DRIVER_UNSUPPORTED",
                ".",
                "Author integration tests require the trusted local minikube Docker driver.",
                exit_code=5,
            )
        images = runner.run(
            tool.path,
            ["image", "ls", "--profile", record.minikube_profile],
            timeout_seconds=30,
        )
        if (
            images.returncode != 0
            or images.truncated
            or not all(image in images.stdout for image in _REQUIRED_IMAGES)
        ):
            return service._issue(
                "AUTHOR_INTEGRATION_IMAGES_MISSING",
                ".",
                "The four fixed KubeLab images must already be cached in local minikube.",
                exit_code=5,
            )
    except (OSError, ValueError, json.JSONDecodeError, ToolExecutionError):
        return service._issue(
            "AUTHOR_INTEGRATION_ENVIRONMENT_UNAVAILABLE",
            ".",
            "The trusted local minikube integration environment is unavailable.",
            exit_code=5,
        )
    except Exception:
        return service._issue(
            "AUTHOR_INTEGRATION_CONTEXT_UNTRUSTED",
            ".",
            "The current minikube Context is untrusted or its identity has drifted.",
            exit_code=5,
        )
    return None


def _docker_profile(payload: Any, profile: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    entries = payload.get("valid")
    if not isinstance(entries, list):
        entries = payload.get("Valid")
    if not isinstance(entries, list):
        return False
    for value in entries:
        if not isinstance(value, Mapping):
            continue
        name = value.get("Name", value.get("name"))
        config = value.get("Config", value.get("config"))
        driver = config.get("Driver", config.get("driver")) if isinstance(config, Mapping) else None
        if name == profile and driver == "docker":
            return True
    return False


def _run_scenario(
    service: AuthoringService,
    source_registry: LabRegistry,
    scenario: _Scenario,
) -> None:  # pragma: no cover - requires separately authorized local minikube
    trust = build_context_trust_service()
    record = trust.assert_trusted_context()
    fingerprint = trusted_context_fingerprint(record)
    config = load_config()
    kubeconfig = resolve_kubeconfig_path(config)
    namespace = f"kubelab-author-{uuid4().hex[:12]}"
    source_documents = source_registry.materialize_for_gateway(scenario.executable).documents
    repair_path = scenario.directory.joinpath(
        *PurePosixPath(scenario.contract.repairs.full.manifest).parts
    )
    repair_documents, repair_issue = service._load_manifest_documents(repair_path)
    if repair_issue is not None:
        raise ValueError("repair source invalid")
    recreate = frozenset(
        (
            change.resource.api_version,
            change.resource.kind,
            change.resource.name,
        )
        for change in scenario.contract.repairs.full.allowed_changes
        if change.operation == "recreate"
    )

    with tempfile.TemporaryDirectory(prefix="kubelab-author-") as temporary_name:
        temporary = Path(temporary_name)
        catalog = temporary / "labs"
        isolated_lab = catalog / scenario.parent.definition.metadata.id
        manifests = isolated_lab / "manifests"
        manifests.mkdir(parents=True)
        definition = scenario.executable.definition.model_dump(mode="python", by_alias=True)
        definition["environment"]["namespace"] = namespace
        definition["environment"]["manifests"] = ["manifests/resources.yaml"]
        (isolated_lab / "lab.yaml").write_text(
            yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        rewritten_source = tuple(_rewrite_namespace(item, namespace) for item in source_documents)
        (manifests / "resources.yaml").write_text(
            yaml.safe_dump_all(
                [dict(item.data) for item in rewritten_source],
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
        isolated_registry = LabRegistry(catalog)
        snapshot = isolated_registry.scan()
        if snapshot.errors or len(snapshot.labs) != 1:
            raise ValueError("isolated Registry rejected scenario")

        state_dir = temporary / "state"
        state_dir.mkdir()
        database = Database(state_dir / "kubelab.db")
        database.initialize()
        validation = ValidationEngine(database.unit_of_work)
        readiness = EnvironmentReadinessService(
            doctor=build_doctor_service(),
            context_trust=trust,
            unit_of_work=database.unit_of_work,
        )

        def gateway_factory(trusted: TrustedContext, context_fingerprint: str) -> KubernetesGateway:
            del trusted, context_fingerprint
            return KubernetesGateway.from_kubeconfig(
                kubeconfig_path=kubeconfig,
                context_name=record.name,
                context_fingerprint=fingerprint,
            )

        manager = LabManager(
            registry=isolated_registry,
            unit_of_work=database.unit_of_work,
            operation_lock=OperationLock(state_dir / "operation.lock"),
            context_trust=trust,
            gateway_factory=gateway_factory,
            validation=validation,
            readiness=readiness,
        )
        session: LabSessionSnapshot | None = None
        try:
            session = manager.start(scenario.parent.definition.metadata.id)
            scope = SessionScope(
                lab_id=session.lab_id,
                session_id=session.id,
                namespace=session.namespace,
                context_fingerprint=fingerprint,
            )
            gateway = gateway_factory(record, fingerprint)
            try:
                rewritten_repair = tuple(
                    _rewrite_namespace(item, namespace) for item in repair_documents
                )
                gateway.apply_authoring_repair(scope, rewritten_repair, recreate=recreate)
            finally:
                gateway.close()
            verified = manager.verify(session.id)
            if verified.status is not ValidationStatus.PASSED:
                raise ValueError("success contract failed")
            manager.reset(session.id)
            manager.cleanup(session.id)
            audit = gateway_factory(record, fingerprint)
            try:
                if audit.namespace_exists(scope):
                    raise ValueError("owned Namespace residue remains")
                if audit.authoring_persistent_volume_residue(scope):
                    raise ValueError("owned PersistentVolume residue remains")
            finally:
                audit.close()
        finally:
            if session is not None:
                try:
                    manager.cleanup(session.id)
                except Exception:
                    pass
            database.dispose()


def _rewrite_namespace(document: ManifestDocument, namespace: str) -> ManifestDocument:
    value = dict(document.data)
    metadata_value = value.get("metadata")
    if not isinstance(metadata_value, Mapping):
        raise ValueError("manifest metadata missing")
    metadata = dict(metadata_value)
    metadata["namespace"] = namespace
    value["metadata"] = metadata
    return ManifestDocument(
        manifest_path=document.manifest_path,
        document_index=document.document_index,
        data=value,
    )


def _write_junit(
    service: AuthoringService,
    path: Path,
    report: AuthoringTestReport,
) -> AuthoringIssue | None:
    candidate = path if path.is_absolute() else service._workspace / path
    try:
        parent = candidate.parent.resolve(strict=True)
        destination = parent / candidate.name
        destination.relative_to(service._workspace)
        if destination.exists() or destination.is_symlink():
            raise ValueError("output exists")
        suite = ElementTree.Element(
            "testsuite",
            name="kubelab-author-integration",
            tests=str(len(report.results)),
            failures=str(sum(not item.passed for item in report.results)),
        )
        for result in report.results:
            case = ElementTree.SubElement(suite, "testcase", name=result.scenario)
            if not result.passed:
                ElementTree.SubElement(
                    case,
                    "failure",
                    message="AUTHOR_INTEGRATION_CONTRACT_FAILED",
                )
        payload = ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    except (OSError, ValueError):
        return service._issue(
            "AUTHOR_JUNIT_OUTPUT_INVALID",
            candidate.name,
            "JUnit output must be a new regular file inside the author workspace.",
            exit_code=2,
        )
    return None


__all__ = ["run_author_integration"]
