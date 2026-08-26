"""CLI contract tests for the complete M1 troubleshooting workflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from kubelab import cli
from kubelab.kubernetes_gateway import (
    EventSummary,
    LogResult,
    PodSummary,
    ResourceSummary,
)
from kubelab.lab_manager import (
    HintResult,
    LabCatalogItem,
    LabCatalogResult,
    LabDetailResult,
    LabManagerError,
    LabProgress,
    RetrospectiveEditState,
    SessionEvents,
    SessionResources,
    SessionStatusResult,
)
from kubelab.runtime import RuntimeEnvironmentError
from kubelab.session_state import (
    LabSessionSnapshot,
    RetrospectiveSnapshot,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
)
from kubelab.validation_engine import PublicCheckResult, ValidationRunResult

runner = CliRunner()


def session(status: SessionStatus = SessionStatus.READY) -> LabSessionSnapshot:
    return LabSessionSnapshot(
        id="123e4567-e89b-42d3-a456-426614174111",
        lab_id="lab-005-image-pull",
        namespace="kubelab-image-pull",
        status=status,
        context_name="minikube",
        context_fingerprint="a" * 64,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        started_at=datetime(2026, 8, 26, tzinfo=UTC),
        completed_at=None,
        reset_count=0,
        last_error_code=None,
        last_error_context=None,
    )


def catalog_item() -> LabCatalogItem:
    return LabCatalogItem(
        id="lab-005-image-pull",
        name="Repair ImagePullBackOff",
        description="Diagnose an invalid image.",
        difficulty="beginner",
        duration_minutes=25,
        category="workloads",
        tags=("pod", "images"),
        progress=LabProgress.NOT_STARTED,
    )


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def list_labs(self, *, category=None, progress=None):
        self.calls.append(("list", category, progress))
        return LabCatalogResult(labs=(catalog_item(),), errors=())

    def show_lab(self, lab_id: str):
        self.calls.append(("show", lab_id))
        return LabDetailResult(
            lab=catalog_item(),
            namespace="kubelab-image-pull",
            task="Repair the Deployment.",
            completion_description="The Pod is Ready.",
            kubernetes_requirement=">=1.31",
            minimum_cpu=2,
            minimum_memory_mib=2048,
            required_addons=(),
            initial_check_types=("pod_status",),
            success_check_types=("pod_status", "container_image"),
            hint_count=3,
            interview_questions=("How did you diagnose it?",),
        )

    def start(self, lab_id: str):
        self.calls.append(("start", lab_id))
        return session()

    def status(self):
        self.calls.append(("status",))
        return SessionStatusResult(
            session=session(SessionStatus.IN_PROGRESS),
            namespace_exists=True,
            namespace_owned=True,
        )

    def resources(self):
        self.calls.append(("resources",))
        return SessionResources(
            session=session(SessionStatus.IN_PROGRESS),
            resources=(
                ResourceSummary(
                    api_version="apps/v1",
                    kind="Deployment",
                    namespace="kubelab-image-pull",
                    name="web",
                    status="Unavailable",
                ),
            ),
            pods=(
                PodSummary(
                    name="web-abc",
                    phase="Pending",
                    ready=False,
                    restart_count=0,
                    containers=(),
                ),
            ),
        )

    def events(self):
        self.calls.append(("events",))
        return SessionEvents(
            session=session(SessionStatus.IN_PROGRESS),
            events=(
                EventSummary(
                    type="Warning",
                    reason="Failed",
                    message="Image pull failed.",
                    involved_kind="Pod",
                    involved_name="web-abc",
                ),
            ),
        )

    def logs(self, pod: str, *, container=None, previous=False, tail_lines=200):
        self.calls.append(("logs", pod, container, previous, tail_lines))
        return LogResult(
            pod=pod,
            container=container,
            previous=previous,
            content="one\ntwo",
            truncated=True,
            line_count=2,
        )

    def verify(self):
        self.calls.append(("verify",))
        return ValidationRunResult(
            id="123e4567-e89b-42d3-a456-426614174222",
            session_id=session().id,
            purpose=VerificationPurpose.MANUAL,
            status=ValidationStatus.FAILED,
            reset_sequence=0,
            checked_at=datetime(2026, 8, 26, tzinfo=UTC),
            duration_ms=20,
            results=(
                PublicCheckResult(
                    check_id="pod-ready",
                    check_type="pod_status",
                    status=ValidationStatus.FAILED,
                    message="The Pod is not Ready yet.",
                    retryable=False,
                    duration_ms=20,
                ),
            ),
        )

    def next_hint(self):
        self.calls.append(("hint",))
        return HintResult(
            session_id=session().id,
            lab_id=session().lab_id,
            level=1,
            total_levels=3,
            content="Inspect Pod events.",
            newly_unlocked=True,
        )

    def session_snapshot(self):
        self.calls.append(("snapshot",))
        return session()

    def reset(self, session_id: str):
        self.calls.append(("reset", session_id))
        return session()

    def cleanup(self, session_id: str):
        self.calls.append(("cleanup", session_id))
        return session(SessionStatus.COMPLETED)

    def retrospective(self):
        self.calls.append(("retrospective",))
        return RetrospectiveEditState(session=session(), retrospective=None)

    def save_retrospective(self, value, session_id: str):
        self.calls.append(("save_retrospective", session_id, value))
        return RetrospectiveSnapshot(
            session_id=session_id,
            **value.model_dump(),
            updated_at=datetime(2026, 8, 26, tzinfo=UTC),
        )


def install_runtime(monkeypatch) -> FakeManager:
    manager = FakeManager()
    runtime = SimpleNamespace(manager=manager, close=lambda: None)
    monkeypatch.setattr(cli, "build_application_runtime", lambda: runtime)
    return manager


def test_catalog_and_show_support_human_and_json(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)

    listed = runner.invoke(cli.app, ["list", "--category", "workloads", "--status", "not_started"])
    shown = runner.invoke(cli.app, ["show", catalog_item().id, "--json"])

    assert listed.exit_code == 0
    assert "Repair ImagePullBackOff" in listed.stdout
    assert manager.calls[0] == ("list", "workloads", LabProgress.NOT_STARTED)
    payload = json.loads(shown.stdout)
    assert payload["lab"]["id"] == catalog_item().id
    assert "content" not in shown.stdout


def test_start_and_status_use_short_active_session_workflow(monkeypatch) -> None:
    install_runtime(monkeypatch)

    started = runner.invoke(cli.app, ["start", catalog_item().id])
    status = runner.invoke(cli.app, ["status", "--json"])

    assert started.exit_code == 0
    assert "kubelab verify" in started.stdout
    assert json.loads(status.stdout)["session"]["status"] == "in_progress"


def test_resources_events_and_logs_render_safe_cluster_views(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)

    resources = runner.invoke(cli.app, ["resources"])
    events = runner.invoke(cli.app, ["events"])
    logs = runner.invoke(
        cli.app,
        ["logs", "web-abc", "--container", "web", "--previous", "--tail", "20"],
    )

    assert resources.exit_code == events.exit_code == logs.exit_code == 0
    assert "Deployment" in resources.stdout
    assert "Image pull failed" in events.stdout
    assert "one\ntwo" in logs.stdout
    assert "truncated" in logs.stderr
    assert ("logs", "web-abc", "web", True, 20) in manager.calls


def test_resources_kind_filter_and_json(monkeypatch) -> None:
    install_runtime(monkeypatch)

    result = runner.invoke(cli.app, ["resources", "--kind", "Pod", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resources"] == []
    assert payload["pods"][0]["name"] == "web-abc"


def test_failed_verify_exits_one_and_hint_reveals_one_level(monkeypatch) -> None:
    install_runtime(monkeypatch)

    verified = runner.invoke(cli.app, ["verify"])
    hint = runner.invoke(cli.app, ["hint", "--json"])

    assert verified.exit_code == 1
    assert "[FAILED] pod-ready" in verified.stdout
    assert hint.exit_code == 0
    assert json.loads(hint.stdout)["level"] == 1


def test_reset_requires_confirmation_and_can_be_cancelled(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)

    cancelled = runner.invoke(cli.app, ["reset"], input="n\n")

    assert cancelled.exit_code == 0
    assert "Cancelled" in cancelled.stdout
    assert not any(call[0] == "reset" for call in manager.calls)


def test_reset_and_cleanup_show_namespace_before_mutation(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)

    reset = runner.invoke(cli.app, ["reset"], input="y\n")
    cleanup = runner.invoke(cli.app, ["cleanup"], input="y\n")

    assert reset.exit_code == cleanup.exit_code == 0
    assert "Namespace: kubelab-image-pull" in reset.stdout
    assert "Cleanup completed" in cleanup.stdout
    assert any(call[0] == "reset" for call in manager.calls)
    assert any(call[0] == "cleanup" for call in manager.calls)


def test_retrospective_uses_prompts_not_external_editor(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)
    answers = "symptom\nimpact\nsteps\ncause\nfix\nprevent\nsummary\n"

    result = runner.invoke(cli.app, ["retrospective", "edit"], input=answers)

    assert result.exit_code == 0
    assert "Retrospective saved" in result.stdout
    saved = next(call for call in manager.calls if call[0] == "save_retrospective")
    assert saved[2].root_cause == "cause"


def test_application_error_has_stable_json_and_exit_code(monkeypatch) -> None:
    manager = install_runtime(monkeypatch)

    def reject():
        raise LabManagerError("SESSION_NOT_FOUND", "No active Session.")

    manager.status = reject  # type: ignore[method-assign]
    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload == {
        "code": "SESSION_NOT_FOUND",
        "context": {},
        "message": "No active Session.",
        "retryable": False,
    }
    assert "traceback" not in result.stderr.lower()


def test_experiment_commands_reject_unsupported_runtime_with_exit_three(monkeypatch) -> None:
    def reject_runtime():
        raise RuntimeEnvironmentError("Run inside WSL2 Ubuntu.")

    monkeypatch.setattr(cli, "build_application_runtime", reject_runtime)

    result = runner.invoke(cli.app, ["list", "--json"])

    assert result.exit_code == 3
    assert json.loads(result.stderr)["code"] == "RUNTIME_PLATFORM_UNSUPPORTED"
