"""KubeLab command-line entry point."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from kubelab import __version__
from kubelab.config import ConfigError, ToolName, get_config_path, set_tool_path
from kubelab.context_trust import (
    ContextError,
    ContextInspection,
    ContextTrustService,
    build_context_trust_service,
)
from kubelab.doctor import HealthStatus, build_doctor_service

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
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"KubeLab {__version__}")
        raise typer.Exit


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
