"""KubeLab command-line entry point."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
import uvicorn

from kubelab import __version__
from kubelab.authoring import AuthoringService
from kubelab.config import ConfigError, ToolName, get_config_path, set_tool_path
from kubelab.context_trust import (
    ContextError,
    ContextInspection,
    ContextTrustService,
    build_context_trust_service,
)
from kubelab.database import DatabaseError
from kubelab.doctor import HealthStatus, build_doctor_service
from kubelab.guided_learning import public_validation_outcome
from kubelab.lab_manager import (
    LabManager,
    LabManagerError,
    LabProgress,
    PracticeMode,
    SessionStatusResult,
)
from kubelab.operation_lock import OperationLockError
from kubelab.package_manager import (
    PackageInfo,
    PackageManager,
    PackageManagerError,
    verify_package_archive,
)
from kubelab.package_state import PackageStatus
from kubelab.repositories import ActiveSessionConflict
from kubelab.runtime import (
    ApplicationRuntime,
    RuntimeEnvironmentError,
    build_application_runtime,
    build_package_runtime,
)
from kubelab.session_state import LabSessionSnapshot, RetrospectiveInput, ValidationStatus
from kubelab.web import WEB_HOST, WEB_PORT, create_app
from kubelab.workspace import WorkspaceError, workspace_environment

app = typer.Typer(
    name="kubelab",
    help="KubeLab local Kubernetes operations practice platform.",
    add_completion=False,
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage local KubeLab configuration.", no_args_is_help=True)
context_app = typer.Typer(
    help="Inspect and trust the local minikube context.", no_args_is_help=True
)
retrospective_app = typer.Typer(help="Record the troubleshooting retrospective.")
workspace_app = typer.Typer(help="Enter the active lab's restricted WSL workspace.")
lab_app = typer.Typer(help="Create, lint, test, inspect, and package lab content.")
package_app = typer.Typer(help="Verify and manage trusted local lab packages.")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(retrospective_app, name="retrospective")
app.add_typer(workspace_app, name="workspace")
app.add_typer(lab_app, name="lab")
app.add_typer(package_app, name="package")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"KubeLab {__version__}")
        raise typer.Exit


def _public_session_payload(
    session: LabSessionSnapshot,
    *,
    practice_mode: PracticeMode,
    scenario_revealed: bool,
) -> dict[str, Any]:
    payload = session.model_dump(
        mode="json",
        exclude_none=True,
        exclude={
            "variant_id",
            "package_sha256",
            "lab_public_snapshot",
            "scenario_public_snapshot",
            "context_fingerprint",
            "last_error_context",
        },
    )
    payload["source"] = payload.pop("lab_source")
    payload["practice_mode"] = practice_mode.value
    payload["scenario_revealed"] = scenario_revealed
    return payload


def _public_status_payload(result: SessionStatusResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json", exclude_none=True, exclude={"session"})
    payload["session"] = _public_session_payload(
        result.session,
        practice_mode=result.practice_mode,
        scenario_revealed=result.scenario_revealed,
    )
    return payload


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the installed KubeLab version and exit.",
        ),
    ] = False,
) -> None:
    """Run KubeLab local Kubernetes practice workflows."""


@app.command("serve")
def serve_command() -> None:
    """Serve the local REST API on the fixed loopback address."""
    uvicorn.run(create_app(), host=WEB_HOST, port=WEB_PORT)


@app.command("doctor")
def doctor_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable report."),
    ] = False,
) -> None:
    """Diagnose the local Docker, minikube, and Kubernetes environment."""
    try:
        report = build_doctor_service().run()
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        typer.echo(report.model_dump_json(indent=2, exclude_none=True))
    else:
        typer.echo(f"KubeLab environment: {report.status.value}")
        for check in report.checks:
            typer.echo(f"[{check.status.value.upper()}] {check.id}: {check.message}")
            if check.remediation:
                typer.echo(f"  Fix: {check.remediation}")

    if report.status is HealthStatus.UNHEALTHY:
        raise typer.Exit(code=3)


@app.command("list")
def list_command(
    category: Annotated[str | None, typer.Option("--category", help="Filter by category.")] = None,
    status: Annotated[
        LabProgress | None,
        typer.Option("--status", help="Filter by learning progress."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List safe local lab metadata and learner progress."""
    with _application(json_output) as manager:
        result = manager.list_labs(category=category, progress=status)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    if not result.labs:
        typer.echo("No labs matched the selected filters.")
    for lab in result.labs:
        typer.echo(
            f"{lab.id:<28} [{lab.progress.value}] {lab.name} "
            f"({lab.difficulty}, {lab.duration_minutes} min)"
        )
    if result.errors:
        typer.echo(f"Warning: {len(result.errors)} invalid lab definition(s) were skipped.")


@app.command("show")
def show_command(
    lab_id: Annotated[str, typer.Argument(help="Lab ID from 'kubelab list'.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the task brief without revealing answers or hint content."""
    with _application(json_output) as manager:
        result = manager.show_lab(lab_id)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    typer.echo(f"{result.lab.name} ({result.lab.id})")
    typer.echo(f"Progress: {result.lab.progress.value}")
    typer.echo(f"Difficulty: {result.lab.difficulty}; duration: {result.lab.duration_minutes} min")
    typer.echo(f"Namespace: {result.namespace}")
    typer.echo(f"Task: {result.task}")
    typer.echo(f"Done when: {result.completion_description}")
    typer.echo(f"Hints: {result.hint_count} level(s); use 'kubelab hint' one at a time.")


@app.command("start")
def start_command(
    lab_id: Annotated[str, typer.Argument(help="Lab ID from 'kubelab list'.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create one faulty lab environment and prove its initial contract."""
    with _application(json_output) as manager:
        session = manager.start(lab_id)
        presentation = manager.session_status_snapshot()
    if json_output:
        typer.echo(
            json.dumps(
                _public_session_payload(
                    session,
                    practice_mode=presentation.practice_mode,
                    scenario_revealed=presentation.scenario_revealed,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(f"Lab ready: {session.lab_id}")
        typer.echo(f"Namespace: {session.namespace}")
        typer.echo("Investigate with kubectl, then run 'kubelab verify'.")


@app.command("status")
def status_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconcile and show the unique active lab Session."""
    with _application(json_output) as manager:
        result = manager.status()
    if json_output:
        typer.echo(json.dumps(_public_status_payload(result), indent=2))
    else:
        typer.echo(f"Lab: {result.session.lab_id}")
        typer.echo(f"Status: {result.session.status.value}")
        typer.echo(f"Namespace: {result.session.namespace}")
        typer.echo(f"Namespace exists: {'yes' if result.namespace_exists else 'no'}")


@app.command("resources")
def resources_command(
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by resource Kind.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show safe summaries of resources in the active lab Namespace."""
    with _application(json_output) as manager:
        result = manager.resources()
        presentation = manager.session_status_snapshot()
    if kind is not None:
        result = result.model_copy(
            update={
                "resources": tuple(
                    item for item in result.resources if item.kind.casefold() == kind.casefold()
                ),
                "pods": result.pods if kind.casefold() == "pod" else (),
            }
        )
    if json_output:
        payload = result.model_dump(mode="json", exclude_none=True)
        payload["session"] = _public_session_payload(
            result.session,
            practice_mode=presentation.practice_mode,
            scenario_revealed=presentation.scenario_revealed,
        )
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"Namespace: {result.session.namespace}")
    for item in result.resources:
        typer.echo(f"{item.kind:<24} {item.name:<36} {item.status or '-'}")
    if not result.resources:
        typer.echo("No resources matched.")


@app.command("events")
def events_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show Kubernetes Events for the active lab Namespace."""
    with _application(json_output) as manager:
        result = manager.events()
        presentation = manager.session_status_snapshot()
    if json_output:
        payload = result.model_dump(mode="json", exclude_none=True)
        payload["session"] = _public_session_payload(
            result.session,
            practice_mode=presentation.practice_mode,
            scenario_revealed=presentation.scenario_revealed,
        )
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"Namespace: {result.session.namespace}")
    for event in result.events:
        target = "/".join(filter(None, (event.involved_kind, event.involved_name)))
        typer.echo(
            f"{event.type or '-':<8} {event.reason or '-':<24} {target} {event.message or ''}"
        )
    if not result.events:
        typer.echo("No Events found.")


@app.command("logs")
def logs_command(
    pod: Annotated[str, typer.Argument(help="Pod name in the active lab Namespace.")],
    container: Annotated[str | None, typer.Option("--container", help="Container name.")] = None,
    previous: Annotated[
        bool, typer.Option("--previous", help="Read the previous container.")
    ] = False,
    tail: Annotated[int, typer.Option("--tail", min=1, max=500)] = 200,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read bounded Pod logs without persisting their content."""
    with _application(json_output) as manager:
        result = manager.logs(
            pod,
            container=container,
            previous=previous,
            tail_lines=tail,
        )
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(result.content)
        if result.truncated:
            typer.echo("[output truncated by KubeLab safety limits]", err=True)


@app.command("verify")
def verify_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Evaluate the active lab success checks."""
    with _application(json_output) as manager:
        result = manager.verify()
    if json_output:
        payload = result.model_dump(mode="json")
        payload["status"] = public_validation_outcome(result.status).value
        for item, check in zip(payload["results"], result.results, strict=True):
            item["status"] = public_validation_outcome(check.status).value
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"Verification: {public_validation_outcome(result.status).value}")
        for check in result.results:
            outcome = public_validation_outcome(check.status).value.upper()
            typer.echo(f"[{outcome}] {check.check_id}: {check.message}")
    if result.status is ValidationStatus.FAILED:
        raise typer.Exit(code=1)
    if result.status is ValidationStatus.ERROR:
        raise typer.Exit(code=5)


@app.command("hint")
def hint_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reveal only the next hint for the active lab."""
    with _application(json_output) as manager:
        result = manager.next_hint()
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        label = "Unlocked" if result.newly_unlocked else "Last available"
        typer.echo(f"{label} hint {result.level}/{result.total_levels}:")
        typer.echo(result.content)


@app.command("reset")
def reset_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete and rebuild the active faulty environment after confirmation."""
    with _application(json_output) as manager:
        session = manager.session_snapshot()
        if not _confirm_destructive("reset", session.namespace, json_output=json_output):
            return
        result = manager.reset(session.id)
        presentation = manager.session_status_snapshot()
    if json_output:
        typer.echo(
            json.dumps(
                _public_session_payload(
                    result,
                    practice_mode=presentation.practice_mode,
                    scenario_revealed=presentation.scenario_revealed,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(f"Lab reset to its initial fault: {result.lab_id}")
        typer.echo(f"Namespace: {result.namespace}")


@app.command("cleanup")
def cleanup_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete the active experiment Namespace after exact ownership checks."""
    with _application(json_output) as manager:
        session = manager.session_snapshot()
        if not _confirm_destructive("delete", session.namespace, json_output=json_output):
            return
        presentation = manager.session_status_snapshot()
        result = manager.cleanup(session.id)
    if json_output:
        typer.echo(
            json.dumps(
                _public_session_payload(
                    result,
                    practice_mode=presentation.practice_mode,
                    scenario_revealed=presentation.scenario_revealed,
                ),
                indent=2,
            )
        )
    else:
        typer.echo(f"Cleanup completed: {result.namespace}")


@retrospective_app.command("edit")
def retrospective_edit_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Edit the active or most recent Session retrospective with safe prompts."""
    with _application(json_output) as manager:
        state = manager.retrospective()
        current = state.retrospective
        value = RetrospectiveInput(
            symptom=typer.prompt("Symptom", default=current.symptom if current else ""),
            impact=typer.prompt("Impact", default=current.impact if current else ""),
            investigation=typer.prompt(
                "Investigation", default=current.investigation if current else ""
            ),
            root_cause=typer.prompt("Root cause", default=current.root_cause if current else ""),
            resolution=typer.prompt("Resolution", default=current.resolution if current else ""),
            prevention=typer.prompt("Prevention", default=current.prevention if current else ""),
            interview_summary=typer.prompt(
                "Interview summary", default=current.interview_summary if current else ""
            ),
        )
        result = manager.save_retrospective(value, state.session.id)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(f"Retrospective saved for Session {result.session_id}.")


@workspace_app.command("enter")
def workspace_enter_command() -> None:
    """Open a fixed Bash shell with short-lived access to only the active Namespace."""
    with _runtime(json_output=False) as runtime:
        with workspace_environment(runtime.manager, runtime.kubeconfig_path) as environment:
            typer.echo(f"Restricted workspace: {environment.namespace}")
            typer.echo("Secret and cluster-scoped access are blocked; exit with Ctrl-D.")
            shell_environment = os.environ.copy()
            shell_environment["KUBECONFIG"] = str(environment.kubeconfig_path)
            shell_environment["KUBELAB_NAMESPACE"] = environment.namespace
            shell_environment["PS1"] = f"kubelab:{environment.namespace} $ "
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-i"],
                check=False,
                env=shell_environment,
            )
            if result.returncode != 0:
                raise typer.Exit(code=result.returncode)


@contextmanager
def _application(json_output: bool) -> Iterator[LabManager]:
    with _runtime(json_output) as runtime:
        yield runtime.manager


@contextmanager
def _runtime(json_output: bool) -> Iterator[ApplicationRuntime]:
    runtime: ApplicationRuntime | None = None
    try:
        runtime = build_application_runtime()
        yield runtime
    except (
        ConfigError,
        DatabaseError,
        ContextError,
        LabManagerError,
        PackageManagerError,
        RuntimeEnvironmentError,
        WorkspaceError,
    ) as exc:
        _raise_application_error(exc, json_output=json_output)
    except ActiveSessionConflict as exc:
        _raise_application_error(exc, json_output=json_output)
    except OperationLockError as exc:
        _raise_application_error(exc, json_output=json_output)
    except typer.Exit:
        raise
    except Exception as exc:
        _emit_error(
            code="INTERNAL_ERROR",
            message="KubeLab could not complete the command.",
            context={},
            retryable=False,
            json_output=json_output,
        )
        raise typer.Exit(code=5) from exc
    finally:
        if runtime is not None:
            runtime.close()


def _raise_application_error(error: Exception, *, json_output: bool) -> NoReturn:
    code = str(getattr(error, "code", "INTERNAL_ERROR"))
    message = str(getattr(error, "message", str(error)))
    context = getattr(error, "context", {})
    if not isinstance(context, dict):
        context = {}
    retryable = bool(getattr(error, "retryable", False))
    explicit_exit_code = getattr(error, "exit_code", None)
    if isinstance(explicit_exit_code, int):
        exit_code = explicit_exit_code
    elif isinstance(error, ConfigError) or code in {"LAB_NOT_FOUND", "LAB_INVALID"}:
        exit_code = 2
    elif isinstance(error, (ContextError, RuntimeEnvironmentError)) or code in {
        "CONTEXT_DRIFT",
        "CONTEXT_NOT_TRUSTED",
        "CONTEXT_NOT_LOCAL_MINIKUBE",
        "RUNTIME_PLATFORM_UNSUPPORTED",
    }:
        exit_code = 3
    elif isinstance(error, (ActiveSessionConflict, OperationLockError)) or code in {
        "ACTIVE_SESSION_CONFLICT",
        "INVALID_SESSION_STATE",
        "SESSION_NOT_FOUND",
        "ENVIRONMENT_REMOVED",
        "OPERATION_IN_PROGRESS",
    }:
        exit_code = 4
    else:
        exit_code = 5
    _emit_error(
        code=code,
        message=message,
        context=context,
        retryable=retryable,
        json_output=json_output,
    )
    raise typer.Exit(code=exit_code) from error


@contextmanager
def _package_application(json_output: bool) -> Iterator[PackageManager]:
    runtime = None
    try:
        runtime = build_package_runtime()
        if runtime.packages is None:
            raise PackageManagerError(
                "PACKAGE_SERVICE_UNAVAILABLE",
                "The local package inventory service is unavailable.",
                exit_code=5,
            )
        yield runtime.packages
    except PackageManagerError as exc:
        _raise_application_error(exc, json_output=json_output)
    except (ConfigError, DatabaseError, OperationLockError) as exc:
        _raise_application_error(exc, json_output=json_output)
    except typer.Exit:
        raise
    except Exception as exc:
        _emit_error(
            code="INTERNAL_ERROR",
            message="KubeLab could not complete the package command.",
            context={},
            retryable=False,
            json_output=json_output,
        )
        raise typer.Exit(code=10) from exc
    finally:
        if runtime is not None:
            runtime.close()


@package_app.command("verify")
def package_verify_command(
    archive: Annotated[Path, typer.Argument(help="Local .kubelab-lab.tar.gz file.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify one archive offline without changing local state."""
    try:
        result = verify_package_archive(archive)
    except PackageManagerError as exc:
        _raise_application_error(exc, json_output=json_output)
    if json_output:
        typer.echo(result.model_dump_json(indent=2, by_alias=True, exclude_none=True))
    else:
        typer.echo(f"Package verified: {result.filename}")
        typer.echo(f"Lab: {result.lab_id}")
        typer.echo(
            f"Format: v{result.format_version}; importable: {'yes' if result.importable else 'no'}"
        )
        typer.echo(f"SHA256: {result.sha256}")
        typer.echo(result.compatibility_message)


@package_app.command("import")
def package_import_command(
    archive: Annotated[Path, typer.Argument(help="Local format v2 lab archive.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Verify and stage one local archive without enabling it."""
    with _package_application(json_output) as packages:
        result = packages.import_archive(archive)
    _emit_package_operation(result, json_output=json_output)


@package_app.command("list")
def package_list_command(
    status: Annotated[PackageStatus | None, typer.Option("--status")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List registered local package versions."""
    with _package_application(json_output) as packages:
        result = packages.list_packages(status)
    if json_output:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json", by_alias=True) for item in result],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not result:
        typer.echo("No local packages are registered.")
    for item in result:
        _echo_package(item)


@package_app.command("show")
def package_show_command(
    lab_id: Annotated[str, typer.Argument(help="Local package experiment ID.")],
    version: Annotated[str | None, typer.Option("--version")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show registered versions and public provenance for one experiment."""
    with _package_application(json_output) as packages:
        result = packages.show(lab_id, version)
    if json_output:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json", by_alias=True) for item in result],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for item in result:
        _echo_package(item)


@package_app.command("enable")
def package_enable_command(
    lab_id: Annotated[str, typer.Argument(help="Local package experiment ID.")],
    version: Annotated[str, typer.Option("--version", help="Exact imported version.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable one exact version for future Sessions."""
    with _package_application(json_output) as packages:
        result = packages.enable(lab_id, version)
    _emit_package_operation(result, json_output=json_output)


@package_app.command("disable")
def package_disable_command(
    lab_id: Annotated[str, typer.Argument(help="Local package experiment ID.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove the enabled version from the new-Session catalogue."""
    with _package_application(json_output) as packages:
        result = packages.disable(lab_id)
    _emit_package_operation(result, json_output=json_output)


@package_app.command("remove")
def package_remove_command(
    lab_id: Annotated[str, typer.Argument(help="Local package experiment ID.")],
    version: Annotated[str, typer.Option("--version", help="Exact registered version.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm destructive removal.")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove one version, deferring file deletion while a Session uses it."""
    if not yes:
        if json_output or not sys.stdin.isatty():
            _emit_error(
                code="PACKAGE_CONFIRMATION_REQUIRED",
                message="Package removal in JSON or non-interactive mode requires --yes.",
                context={"lab_id": lab_id, "package_version": version},
                retryable=False,
                json_output=json_output,
            )
            raise typer.Exit(code=2)
        if not typer.confirm(f"Remove local package {lab_id} {version}?", default=False):
            typer.echo("Cancelled; no package state was changed.")
            return
    with _package_application(json_output) as packages:
        result = packages.remove(lab_id, version)
    _emit_package_operation(result, json_output=json_output)


def _emit_package_operation(result: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(result.model_dump_json(indent=2, by_alias=True, exclude_none=True))
        return
    typer.echo(f"Package {result.action}: {result.package.lab_id} {result.package.package_version}")
    typer.echo(f"Status: {result.package.status.value}")
    if result.idempotent:
        typer.echo("No change was required.")
    if result.deferred:
        typer.echo("File removal is deferred until the active Session completes.")


def _echo_package(item: PackageInfo) -> None:
    verified = "verified" if item.publisher_verified else "publisher-unverified"
    typer.echo(
        f"{item.lab_id} {item.package_version} [{item.status.value}] "
        f"{item.publisher_name} ({verified}, {item.integrity.value})"
    )


def _emit_error(
    *,
    code: str,
    message: str,
    context: dict[str, Any],
    retryable: bool,
    json_output: bool,
) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "code": code,
                    "message": message,
                    "context": context,
                    "retryable": retryable,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            err=True,
        )
    else:
        typer.echo(f"KubeLab error [{code}]: {message}", err=True)


def _confirm_destructive(action: str, namespace: str, *, json_output: bool) -> bool:
    typer.echo(f"Namespace: {namespace}", err=json_output)
    confirmed = typer.confirm(f"Confirm {action} of this experiment environment?", default=False)
    if not confirmed:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "cancelled": True,
                        "namespace": namespace,
                        "operation": action,
                    },
                    sort_keys=True,
                )
            )
        else:
            typer.echo("Cancelled; no cluster changes were made.")
    return confirmed


@lab_app.command("init")
def lab_init_command(
    target: Annotated[Path, typer.Argument(help="New lab directory, or variant parent lab.")],
    scenario_type: Annotated[str, typer.Option("--type", help="baseline, variant, or composite.")],
    scenario_id: Annotated[str, typer.Option("--id", help="Explicit lab or variant ID.")],
    title: Annotated[str | None, typer.Option("--title", help="Lab title or variant name.")] = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    difficulty: Annotated[str | None, typer.Option("--difficulty")] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generate a complete safe sample without overwriting author files."""
    title = _author_value(title, "Title", json_output=json_output)
    description = _author_value(description, "Description", json_output=json_output)
    if scenario_type == "variant":
        category = category or "workload"
        difficulty = difficulty or "intermediate"
    else:
        category = _author_value(category, "Category", json_output=json_output)
        difficulty = _author_value(difficulty, "Difficulty", json_output=json_output)
    service = _authoring_service(workspace, json_output=json_output)
    report = service.init(
        target,
        scenario_type=scenario_type,
        scenario_id=scenario_id,
        title=title,
        category=category,
        difficulty=difficulty,
        description=description,
        dry_run=dry_run,
    )
    _emit_author_report(report, json_output=json_output)


@lab_app.command("lint")
def lab_lint_command(
    target: Annotated[Path, typer.Argument(help="Lab, variant, or catalogue directory.")],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run runtime-compatible schema, safety, teaching, and repair checks."""
    report = _authoring_service(workspace, json_output=json_output).lint(target)
    _emit_author_report(report, json_output=json_output)


@lab_app.command("test")
def lab_test_command(
    target: Annotated[Path, typer.Argument(help="Lab, variant, or catalogue directory.")],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    integration: Annotated[
        bool, typer.Option("--integration", help="Use the explicitly gated local minikube.")
    ] = False,
    junit: Annotated[Path | None, typer.Option("--junit")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Prove scenario lifecycles with Fake observations by default."""
    service = _authoring_service(workspace, json_output=json_output)
    if integration:
        report = service.test_integration(target, junit=junit)
    else:
        if junit is not None:
            _emit_error(
                code="AUTHOR_JUNIT_REQUIRES_INTEGRATION",
                message="--junit is available only with --integration.",
                context={},
                retryable=False,
                json_output=json_output,
            )
            raise typer.Exit(code=2)
        report = service.test(target)
    _emit_author_report(report, json_output=json_output)


@lab_app.command("inspect")
def lab_inspect_command(
    target: Annotated[Path, typer.Argument(help="Lab, variant, or catalogue directory.")],
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Preview bounded resources, repair paths, disclosures, and file hashes."""
    report = _authoring_service(workspace, json_output=json_output).inspect(target)
    _emit_author_report(report, json_output=json_output)


@lab_app.command("package")
def lab_package_command(
    target: Annotated[Path, typer.Argument(help="One complete lab-family directory.")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build and verify a deterministic, non-installing author package."""
    report = _authoring_service(workspace, json_output=json_output).package(target, output=output)
    _emit_author_report(report, json_output=json_output)


def _authoring_service(workspace: Path, *, json_output: bool) -> AuthoringService:
    try:
        return AuthoringService(workspace)
    except Exception as exc:
        _emit_error(
            code="AUTHOR_WORKSPACE_INVALID",
            message="The author workspace is not a readable local directory.",
            context={},
            retryable=False,
            json_output=json_output,
        )
        raise typer.Exit(code=2) from exc


def _author_value(value: str | None, label: str, *, json_output: bool) -> str:
    if value is not None and value.strip():
        return value.strip()
    if sys.stdin.isatty() and not json_output:
        entered = str(typer.prompt(label)).strip()
        if entered:
            return entered
    _emit_error(
        code="AUTHOR_INPUT_REQUIRED",
        message=f"{label} is required in non-interactive mode.",
        context={},
        retryable=False,
        json_output=json_output,
    )
    raise typer.Exit(code=2)


def _emit_author_report(report: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(report.model_dump_json(indent=2, by_alias=True, exclude_none=True))
    else:
        outcome = "passed" if report.passed else "failed"
        typer.echo(f"Authoring {report.command}: {outcome}")
        for issue in report.issues:
            location = issue.relative_path
            if issue.field_path:
                location += f":{issue.field_path}"
            typer.echo(f"[{issue.severity.value}] {issue.code} {location}: {issue.message}")
        if hasattr(report, "files"):
            for path in report.files:
                typer.echo(f"  {path}")
        if getattr(report, "output", None):
            typer.echo(f"Output: {report.output}")
            typer.echo(f"SHA256: {report.sha256}")
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


@config_app.command("set-tool")
def config_set_tool(
    tool: Annotated[ToolName, typer.Argument(help="Tool name to configure.")],
    executable: Annotated[Path, typer.Argument(help="Absolute executable path.")],
) -> None:
    """Set an explicit absolute path for a supported local tool."""
    config_path = get_config_path()
    try:
        set_tool_path(config_path, tool, executable)
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Configured {tool.value}: {executable.resolve()}")
    typer.echo(f"Saved to: {config_path}")


@context_app.command("inspect")
def context_inspect_command(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable identity."),
    ] = False,
) -> None:
    """Inspect the current Kubernetes identity without changing any resource."""
    service = _build_context_service()
    try:
        inspection = service.inspect()
    except ContextError as exc:
        _raise_context_error(exc)
    except ConfigError as exc:
        _raise_config_error(exc)
    if json_output:
        typer.echo(inspection.model_dump_json(indent=2))
    else:
        _render_context_inspection(inspection)


@context_app.command("trust")
def context_trust_command() -> None:
    """Trust the current context after proving it is a running local minikube profile."""
    service = _build_context_service()
    try:
        record = service.trust()
    except ContextError as exc:
        _raise_context_error(exc)
    except ConfigError as exc:
        _raise_config_error(exc)
    typer.echo(f"Trusted context: {record.name}")
    typer.echo(f"minikube profile: {record.minikube_profile}")
    typer.echo(f"API Server: {record.server}")
    typer.echo(f"CA SHA256: {record.ca_sha256}")
    typer.echo(f"kube-system UID: {record.kube_system_uid}")


@context_app.command("untrust")
def context_untrust_command() -> None:
    """Remove local trust for the current context without changing the cluster."""
    service = _build_context_service()
    try:
        context_name, removed = service.untrust()
    except ContextError as exc:
        _raise_context_error(exc)
    except ConfigError as exc:
        _raise_config_error(exc)
    if removed:
        typer.echo(f"Removed trust for context: {context_name}")
    else:
        typer.echo(f"Context was not trusted: {context_name}")


def _build_context_service() -> ContextTrustService:
    try:
        return build_context_trust_service()
    except ConfigError as exc:
        _raise_config_error(exc)


def _raise_config_error(error: ConfigError) -> NoReturn:
    typer.echo(f"Configuration error: {error}", err=True)
    raise typer.Exit(code=2) from error


def _raise_context_error(error: ContextError) -> NoReturn:
    typer.echo(f"Context error [{error.code}]: {error}", err=True)
    raise typer.Exit(code=3) from error


def _render_context_inspection(inspection: ContextInspection) -> None:
    typer.echo(f"Context: {inspection.context_name}")
    typer.echo(f"minikube profile: {inspection.minikube_profile or 'not detected'}")
    typer.echo(f"API Server: {inspection.api_server}")
    typer.echo(f"CA SHA256: {inspection.ca_sha256}")
    typer.echo(f"kube-system UID: {inspection.kube_system_uid}")
    typer.echo(f"Kubernetes Server: {inspection.kubernetes_version}")
    typer.echo(f"Trusted: {'yes' if inspection.trusted else 'no'}")
    typer.echo(f"Trust state: {inspection.trust_state.value}")
