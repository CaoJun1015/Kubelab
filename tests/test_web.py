"""REST API contract tests using only a fake application service."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kubelab.config import ConfigError
from kubelab.context_trust import ContextNotTrustedError
from kubelab.database import DatabaseError
from kubelab.guided_learning import (
    EnvironmentReadinessReport,
    OnboardingState,
    ReadinessCheck,
    ReadinessCheckStatus,
    ReadinessStatus,
)
from kubelab.kubernetes_gateway import EventSummary, LogResult, PodSummary, ResourceSummary
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
from kubelab.operation_lock import OperationLockError
from kubelab.repositories import ActiveSessionConflict
from kubelab.runtime import RuntimeEnvironmentError
from kubelab.session_state import (
    LabSessionSnapshot,
    RetrospectiveInput,
    RetrospectiveSnapshot,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
)
from kubelab.validation_engine import PublicCheckResult, ValidationRunResult
from kubelab.web import (
    CSRF_COOKIE,
    CSRF_HEADER,
    REQUEST_ID_HEADER,
    WEB_ORIGIN,
    EnvironmentResponse,
    KubeLabApplicationService,
    _safe_context,
    _status_for_error,
    create_app,
)

NOW = datetime(2026, 8, 26, tzinfo=UTC)
SESSION_ID = "123e4567-e89b-42d3-a456-426614174111"
RUN_ID = "123e4567-e89b-42d3-a456-426614174222"
NAMESPACE = "kubelab-image-pull"


def session(status: SessionStatus = SessionStatus.IN_PROGRESS) -> LabSessionSnapshot:
    return LabSessionSnapshot(
        id=SESSION_ID,
        lab_id="lab-005-image-pull",
        namespace=NAMESPACE,
        status=status,
        context_name="minikube",
        context_fingerprint="a" * 64,
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW if status is SessionStatus.COMPLETED else None,
        reset_count=1,
        last_error_code=None,
        last_error_context={"credential": "must-not-be-public"},
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
        progress=LabProgress.ACTIVE,
    )


class FakeApplicationService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        self.error: Exception | None = None

    def _raise_if_requested(self) -> None:
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True

    def environment(self) -> EnvironmentResponse:
        self._raise_if_requested()
        return EnvironmentResponse(process_platform="Linux", wsl_distribution="Ubuntu")

    def onboarding(self) -> OnboardingState:
        self.calls.append(("onboarding",))
        return OnboardingState(first_use=True, completed_at=None, report=None)

    def check_environment(self) -> EnvironmentReadinessReport:
        self.calls.append(("check_environment",))
        return EnvironmentReadinessReport(
            status=ReadinessStatus.BLOCKED,
            checks=(
                ReadinessCheck(
                    id="docker_daemon",
                    status=ReadinessCheckStatus.FAIL,
                    message="Docker daemon is unavailable.",
                    remediation="启动Docker Engine并重新检查。",
                    commands=("docker info",),
                ),
            ),
            generated_at=NOW,
        )

    def list_labs(self, *, category=None, progress=None) -> LabCatalogResult:
        self.calls.append(("list", category, progress))
        return LabCatalogResult(labs=(catalog_item(),), errors=())

    def show_lab(self, lab_id: str) -> LabDetailResult:
        self.calls.append(("show", lab_id))
        return LabDetailResult(
            lab=catalog_item(),
            namespace=NAMESPACE,
            task="Repair the Deployment.",
            completion_description="The Pod is Ready.",
            kubernetes_requirement=">=1.31",
            minimum_cpu=2,
            minimum_memory_mib=2048,
            required_addons=(),
            initial_check_types=("pod_status",),
            success_check_types=("pod_status",),
            hint_count=3,
            interview_questions=("How did you diagnose it?",),
        )

    def active_session(self) -> SessionStatusResult:
        self._raise_if_requested()
        self.calls.append(("active",))
        return SessionStatusResult(session=session(), namespace_exists=True, namespace_owned=True)

    def start(self, lab_id: str) -> LabSessionSnapshot:
        self.calls.append(("start", lab_id))
        return session(SessionStatus.READY)

    def resources(self) -> SessionResources:
        self.calls.append(("resources",))
        return SessionResources(
            session=session(),
            resources=(
                ResourceSummary(
                    api_version="v1",
                    kind="Secret",
                    namespace=NAMESPACE,
                    name="app-credentials",
                    secret_type="Opaque",
                    secret_keys=("password",),
                ),
            ),
            pods=(
                PodSummary(
                    name="web-abc",
                    phase="Running",
                    ready=True,
                    restart_count=0,
                    containers=(),
                ),
            ),
        )

    def events(self) -> SessionEvents:
        self.calls.append(("events",))
        return SessionEvents(
            session=session(),
            events=(
                EventSummary(
                    type="Warning",
                    reason="Failed",
                    message="Image pull failed; token=event-secret",
                    involved_kind="Pod",
                    involved_name="web-abc",
                ),
            ),
        )

    def logs(self, pod: str, *, container=None, previous=False, tail_lines=200) -> LogResult:
        self.calls.append(("logs", pod, container, previous, tail_lines))
        return LogResult(
            pod=pod,
            container=container,
            previous=previous,
            content="bounded output password=log-secret",
            truncated=False,
            line_count=1,
        )

    def verify(self) -> ValidationRunResult:
        self.calls.append(("verify",))
        return ValidationRunResult(
            id=RUN_ID,
            session_id=SESSION_ID,
            purpose=VerificationPurpose.MANUAL,
            status=ValidationStatus.FAILED,
            reset_sequence=1,
            checked_at=NOW,
            duration_ms=12,
            results=(
                PublicCheckResult(
                    check_id="pod-ready",
                    check_type="pod_status",
                    status=ValidationStatus.FAILED,
                    message="The Pod is not Ready yet.",
                    retryable=False,
                    duration_ms=12,
                ),
            ),
        )

    def next_hint(self) -> HintResult:
        self.calls.append(("hint",))
        return HintResult(
            session_id=SESSION_ID,
            lab_id=session().lab_id,
            level=1,
            total_levels=3,
            content="Inspect Pod events.",
            newly_unlocked=True,
        )

    def reset(self, session_id: str) -> LabSessionSnapshot:
        self.calls.append(("reset", session_id))
        return session(SessionStatus.READY)

    def cleanup(self, session_id: str) -> LabSessionSnapshot:
        self.calls.append(("cleanup", session_id))
        return session(SessionStatus.COMPLETED)

    def retrospective(self) -> RetrospectiveEditState:
        self.calls.append(("retrospective",))
        return RetrospectiveEditState(session=session(), retrospective=None)

    def save_retrospective(
        self, value: RetrospectiveInput, session_id: str
    ) -> RetrospectiveSnapshot:
        self.calls.append(("save_retrospective", session_id, value))
        return RetrospectiveSnapshot(session_id=session_id, updated_at=NOW, **value.model_dump())


@pytest.fixture
def fake() -> FakeApplicationService:
    return FakeApplicationService()


@pytest.fixture
def client(fake: FakeApplicationService):
    with TestClient(create_app(lambda: fake), raise_server_exceptions=False) as value:
        yield value


def csrf(client: TestClient) -> dict[str, str]:
    response = client.get("/health")
    return {"Origin": WEB_ORIGIN, CSRF_HEADER: response.headers[CSRF_HEADER]}


def test_health_issues_strict_httponly_csrf_cookie_and_has_no_cors(
    client: TestClient, fake: FakeApplicationService
) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers[REQUEST_ID_HEADER]
    assert response.headers[CSRF_HEADER]
    cookie = response.headers["set-cookie"].lower()
    assert f"{CSRF_COOKIE}=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "access-control-allow-origin" not in response.headers
    assert not fake.closed


def test_lifespan_closes_shared_application_service(fake: FakeApplicationService) -> None:
    with TestClient(create_app(lambda: fake)) as active_client:
        assert active_client.get("/health").status_code == 200

    assert fake.closed


def test_read_endpoints_delegate_to_fake_application_service(client: TestClient) -> None:
    environment = client.get("/api/v1/environment")
    labs = client.get("/api/v1/labs?category=workloads&progress=active")
    detail = client.get("/api/v1/labs/lab-005-image-pull")
    active = client.get("/api/v1/sessions/active")
    resources = client.get("/api/v1/sessions/active/resources")
    events = client.get("/api/v1/sessions/active/events")
    logs = client.get(
        "/api/v1/sessions/active/logs?pod=web-abc&container=web&previous=true&tail=20"
    )
    retrospective = client.get("/api/v1/sessions/latest/retrospective")
    onboarding = client.get("/api/v1/onboarding")

    assert environment.json()["bind_host"] == "127.0.0.1"
    assert labs.json()["labs"][0]["id"] == "lab-005-image-pull"
    assert labs.json()["invalid_lab_count"] == 0
    assert detail.json()["hint_count"] == 3
    assert active.json()["session"]["status"] == "in_progress"
    resource_text = resources.text.lower()
    assert resources.json()["resources"][0]["secret_keys"] == ["password"]
    assert "must-not-be-public" not in resource_text
    assert "context_fingerprint" not in resource_text
    assert events.json()["events"][0]["reason"] == "Failed"
    assert "event-secret" not in events.text
    assert logs.json()["content"] == "bounded output password=[REDACTED]"
    assert retrospective.json()["retrospective"] is None
    assert onboarding.json() == {"first_use": True, "completed_at": None, "report": None}


def test_onboarding_page_is_static_and_explicit_check_requires_csrf(
    client: TestClient, fake: FakeApplicationService
) -> None:
    page = client.get("/onboarding")
    assert page.status_code == 200
    assert "准备本地实验环境" in page.text
    assert ("check_environment",) not in fake.calls

    rejected = client.post("/api/v1/onboarding/check")
    checked = client.post("/api/v1/onboarding/check", headers=csrf(client))
    assert rejected.status_code == 403
    assert checked.status_code == 200
    assert checked.json()["status"] == "blocked"
    assert checked.json()["checks"][0]["commands"] == ["docker info"]
    assert fake.calls.count(("check_environment",)) == 1


def test_write_endpoints_require_csrf_and_delegate_with_safe_public_results(
    client: TestClient, fake: FakeApplicationService
) -> None:
    headers = csrf(client)
    started = client.post("/api/v1/labs/lab-005-image-pull/start", headers=headers)
    verified = client.post("/api/v1/sessions/active/verify", headers=headers)
    hinted = client.post("/api/v1/sessions/active/hint", headers=headers)
    reset = client.post(
        "/api/v1/sessions/active/reset", headers=headers, json={"namespace": NAMESPACE}
    )
    cleanup = client.post(
        "/api/v1/sessions/active/cleanup", headers=headers, json={"namespace": NAMESPACE}
    )
    saved = client.put(
        "/api/v1/sessions/latest/retrospective",
        headers=headers,
        json={
            "symptom": "Pod pending token=retrospective-secret",
            "root_cause": "Invalid image",
        },
    )

    assert started.status_code == 201
    assert started.json()["status"] == "ready"
    assert "context_name" not in started.text
    assert verified.status_code == 200
    assert verified.json()["status"] == "failed"
    assert "expected" not in verified.text.lower()
    assert "actual" not in verified.text.lower()
    assert hinted.json()["level"] == 1
    assert reset.json()["namespace"] == NAMESPACE
    assert cleanup.json()["status"] == "completed"
    assert saved.json()["root_cause"] == "Invalid image"
    assert "retrospective-secret" not in saved.text
    assert ("reset", SESSION_ID) in fake.calls
    assert ("cleanup", SESSION_ID) in fake.calls


def test_cross_origin_missing_origin_and_bad_csrf_are_rejected(client: TestClient) -> None:
    cross_origin = client.get("/api/v1/labs", headers={"Origin": "http://evil.example"})
    missing_origin = client.post("/api/v1/sessions/active/verify")
    token = client.get("/health").headers[CSRF_HEADER]
    bad_csrf = client.post(
        "/api/v1/sessions/active/verify",
        headers={"Origin": WEB_ORIGIN, CSRF_HEADER: token + "bad"},
    )

    assert cross_origin.status_code == 403
    assert cross_origin.json()["code"] == "ORIGIN_REJECTED"
    assert missing_origin.json()["code"] == "ORIGIN_REQUIRED"
    assert bad_csrf.json()["code"] == "CSRF_TOKEN_INVALID"
    for response in (cross_origin, missing_origin, bad_csrf):
        assert set(response.json()) == {"code", "message", "context", "retryable"}
        assert REQUEST_ID_HEADER in response.headers


def test_namespace_confirmation_and_request_schema_are_enforced(client: TestClient) -> None:
    headers = csrf(client)
    mismatch = client.post(
        "/api/v1/sessions/active/reset",
        headers=headers,
        json={"namespace": "kubelab-other"},
    )
    invalid = client.post(
        "/api/v1/sessions/active/cleanup",
        headers=headers,
        json={"namespace": "production"},
    )
    invalid_log = client.get("/api/v1/sessions/active/logs?pod=../../secret")

    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "NAMESPACE_CONFIRMATION_MISMATCH"
    assert mismatch.json()["context"] == {"namespace": NAMESPACE}
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "REQUEST_VALIDATION_FAILED"
    assert invalid_log.status_code == 422


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LabManagerError("LAB_NOT_FOUND", "not found"), 404, "LAB_NOT_FOUND"),
        (ContextNotTrustedError("not trusted"), 403, "CONTEXT_NOT_TRUSTED"),
        (LabManagerError("INVALID_SESSION_STATE", "invalid"), 409, "INVALID_SESSION_STATE"),
        (LabManagerError("LAB_INVALID", "invalid lab"), 422, "LAB_INVALID"),
        (DatabaseError("database unavailable"), 503, "DATABASE_ERROR"),
        (ConfigError("bad config"), 500, "INTERNAL_ERROR"),
        (OperationLockError("locked"), 409, "OPERATION_IN_PROGRESS"),
        (RuntimeEnvironmentError("not WSL"), 503, "RUNTIME_PLATFORM_UNSUPPORTED"),
    ],
)
def test_application_errors_use_safe_uniform_shape(
    fake: FakeApplicationService, error: Exception, status: int, code: str
) -> None:
    fake.error = error
    with TestClient(create_app(lambda: fake), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/environment")

    assert response.status_code == status
    assert response.json()["code"] == code
    assert set(response.json()) == {"code", "message", "context", "retryable"}
    assert "traceback" not in response.text.lower()


def test_internal_errors_and_not_found_never_expose_exception_details(
    fake: FakeApplicationService,
) -> None:
    fake.error = RuntimeError("token=secret stack=/private/path")
    with TestClient(create_app(lambda: fake), raise_server_exceptions=False) as client:
        failure = client.get("/api/v1/environment")
        missing = client.get("/does-not-exist")

    assert failure.status_code == 500
    assert failure.json() == {
        "code": "INTERNAL_ERROR",
        "message": "KubeLab could not complete the request.",
        "context": {},
        "retryable": False,
    }
    assert "secret" not in failure.text
    assert missing.status_code == 404
    assert missing.json()["code"] == "RESOURCE_NOT_FOUND"


def test_active_session_conflict_only_exposes_whitelisted_session_fields(
    fake: FakeApplicationService,
) -> None:
    fake.error = ActiveSessionConflict(session())
    with TestClient(create_app(lambda: fake), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/environment")

    assert response.status_code == 409
    assert response.json()["context"] == {
        "session_id": SESSION_ID,
        "status": "in_progress",
    }
    assert "fingerprint" not in response.text


def test_error_helpers_whitelist_context_and_cover_status_categories() -> None:
    assert _safe_context(
        {
            "operation": "verify",
            "retry_after_seconds": 2,
            "token": "secret",
            "manifest": {"kind": "Secret"},
        }
    ) == {"operation": "verify", "retry_after_seconds": 2}
    assert _safe_context("not-a-dict") == {}
    assert _status_for_error("KUBERNETES_NOT_FOUND") == 404
    assert _status_for_error("KUBERNETES_FORBIDDEN") == 403
    assert _status_for_error("ACTIVE_SESSION_CONFLICT") == 409
    assert _status_for_error("LAB_INVALID") == 422
    assert _status_for_error("KUBERNETES_TIMEOUT") == 503
    assert _status_for_error("UNCLASSIFIED") == 500


class DelegatingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def list_labs(self, **kwargs: object) -> LabCatalogResult:
        self.calls.append(("list", kwargs))
        return LabCatalogResult(labs=(), errors=())

    def show_lab(self, lab_id: str) -> Any:
        self.calls.append(("show", lab_id))
        return "detail"

    def status(self) -> Any:
        self.calls.append(("status",))
        return "status"

    def start(self, lab_id: str) -> Any:
        self.calls.append(("start", lab_id))
        return "started"

    def resources(self) -> Any:
        self.calls.append(("resources",))
        return "resources"

    def events(self) -> Any:
        self.calls.append(("events",))
        return "events"

    def logs(self, pod: str, **kwargs: object) -> Any:
        self.calls.append(("logs", pod, kwargs))
        return "logs"

    def verify(self) -> Any:
        self.calls.append(("verify",))
        return "verify"

    def next_hint(self) -> Any:
        self.calls.append(("hint",))
        return "hint"

    def reset(self, session_id: str) -> Any:
        self.calls.append(("reset", session_id))
        return "reset"

    def cleanup(self, session_id: str) -> Any:
        self.calls.append(("cleanup", session_id))
        return "cleanup"

    def retrospective(self) -> Any:
        self.calls.append(("retrospective",))
        return "retrospective"

    def save_retrospective(self, value: RetrospectiveInput, session_id: str) -> Any:
        self.calls.append(("save", value, session_id))
        return "saved"


def test_production_web_adapter_delegates_only_to_application_manager(monkeypatch) -> None:
    manager = DelegatingManager()
    runtime = SimpleNamespace(manager=manager, close=lambda: manager.calls.append(("close",)))
    service = KubeLabApplicationService(runtime)  # type: ignore[arg-type]
    value = RetrospectiveInput(symptom="symptom")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    assert service.environment().wsl_distribution == "Ubuntu"
    service.list_labs(category="workloads", progress=LabProgress.ACTIVE)
    assert service.show_lab("lab-id") == "detail"
    assert service.active_session() == "status"
    assert service.start("lab-id") == "started"
    assert service.resources() == "resources"
    assert service.events() == "events"
    assert service.logs("pod", container="web", previous=True, tail_lines=20) == "logs"
    assert service.verify() == "verify"
    assert service.next_hint() == "hint"
    assert service.reset(SESSION_ID) == "reset"
    assert service.cleanup(SESSION_ID) == "cleanup"
    assert service.retrospective() == "retrospective"
    assert service.save_retrospective(value, SESSION_ID) == "saved"
    service.close()
    assert manager.calls[-1] == ("close",)
