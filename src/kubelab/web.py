"""Local-only FastAPI adapter for the KubeLab application service."""

from __future__ import annotations

import hmac
import os
import platform
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from kubelab import __version__
from kubelab.config import ConfigError
from kubelab.context_trust import ContextError
from kubelab.database import DatabaseError
from kubelab.guided_learning import (
    EnvironmentNotReadyError,
    EnvironmentReadinessReport,
    OnboardingState,
    public_validation_outcome,
)
from kubelab.kubernetes_gateway import EventSummary, LogResult
from kubelab.lab_manager import (
    HintResult,
    LabCatalogItem,
    LabCatalogResult,
    LabDetailResult,
    LabManager,
    LabManagerError,
    LabProgress,
    LearningProgressReport,
    RetrospectiveEditState,
    RetrospectiveMetadata,
    SessionEvents,
    SessionResources,
    SessionStatusResult,
    SessionTimeline,
)
from kubelab.operation_lock import OperationLockError
from kubelab.redaction import redact_json
from kubelab.repositories import ActiveSessionConflict
from kubelab.runtime import ApplicationRuntime, RuntimeEnvironmentError, build_application_runtime
from kubelab.session_state import (
    LabSessionSnapshot,
    RetrospectiveInput,
    RetrospectiveSnapshot,
    SessionStatus,
)
from kubelab.validation_engine import ValidationRunResult

WEB_HOST = "127.0.0.1"
WEB_PORT = 8765
WEB_ORIGIN = f"http://{WEB_HOST}:{WEB_PORT}"
CSRF_COOKIE = "kubelab_csrf"
CSRF_HEADER = "X-CSRF-Token"
REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_RESOURCE_KINDS = frozenset(
    {
        "ConfigMap",
        "CronJob",
        "DaemonSet",
        "Deployment",
        "Job",
        "PersistentVolumeClaim",
        "Pod",
        "ReplicaSet",
        "Service",
        "StatefulSet",
    }
)
_PACKAGE_DIR = Path(__file__).resolve().parent
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
)
_PUBLIC_CONTEXT_KEYS = frozenset(
    {
        "already_absent",
        "blocking_check_count",
        "code",
        "field",
        "field_path",
        "lab_id",
        "namespace",
        "operation",
        "retry_after_seconds",
        "session_id",
        "status",
    }
)


class WebModel(BaseModel):
    """Strict immutable model used at the HTTP trust boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(WebModel):
    status: Literal["ok"] = "ok"
    version: str


class EnvironmentResponse(WebModel):
    supported_runtime: Literal["WSL2 Ubuntu"] = "WSL2 Ubuntu"
    process_platform: str
    wsl_distribution: str | None
    bind_host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Literal[8765] = 8765


class PublicSession(WebModel):
    id: str
    lab_id: str
    namespace: str
    status: SessionStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reset_count: int
    last_error_code: str | None


class ActiveSessionResponse(WebModel):
    session: PublicSession
    namespace_exists: bool | None
    namespace_owned: bool | None
    cluster_state: str
    stage: str
    workspace_command: str


class LabsResponse(WebModel):
    labs: tuple[LabCatalogItem, ...]
    invalid_lab_count: int


class PublicResourceSummary(WebModel):
    kind: str
    name: str
    status: str | None


class PublicPodSummary(WebModel):
    name: str
    phase: str | None
    ready: bool
    restart_count: int
    reason: str | None


class ResourcesResponse(WebModel):
    session: PublicSession
    resources: tuple[PublicResourceSummary, ...]
    pods: tuple[PublicPodSummary, ...]


class EventsResponse(WebModel):
    session: PublicSession
    events: tuple[EventSummary, ...]


class RetrospectiveResponse(WebModel):
    session: PublicSession
    retrospective: RetrospectiveSnapshot | None
    metadata: RetrospectiveMetadata | None


class PublicVerificationCheck(WebModel):
    check_id: str
    check_type: str
    status: str
    message: str
    retryable: bool
    duration_ms: int


class VerificationResponse(WebModel):
    id: str
    session_id: str
    status: str
    checked_at: datetime
    duration_ms: int
    results: tuple[PublicVerificationCheck, ...]


class ConfirmationRequest(WebModel):
    namespace: str = Field(pattern=r"^kubelab-[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")


class ErrorResponse(WebModel):
    code: str
    message: str
    context: dict[str, JsonValue] = Field(default_factory=dict)
    retryable: bool = False


class WebApplicationService(Protocol):
    """Only business interface visible to HTTP route handlers."""

    def close(self) -> None: ...

    def environment(self) -> EnvironmentResponse: ...

    def onboarding(self) -> OnboardingState: ...

    def check_environment(self) -> EnvironmentReadinessReport: ...

    def list_labs(
        self, *, category: str | None = None, progress: LabProgress | None = None
    ) -> LabCatalogResult: ...

    def show_lab(self, lab_id: str) -> LabDetailResult: ...

    def active_session(self) -> SessionStatusResult: ...

    def reconcile_active_session(self) -> SessionStatusResult: ...

    def timeline(self) -> SessionTimeline: ...

    def progress(self) -> LearningProgressReport: ...

    def start(self, lab_id: str) -> LabSessionSnapshot: ...

    def resources(self) -> SessionResources: ...

    def events(self) -> SessionEvents: ...

    def logs(
        self,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 200,
    ) -> LogResult: ...

    def verify(self) -> ValidationRunResult: ...

    def next_hint(self) -> HintResult: ...

    def reset(self, session_id: str) -> LabSessionSnapshot: ...

    def cleanup(self, session_id: str) -> LabSessionSnapshot: ...

    def retrospective(self) -> RetrospectiveEditState: ...

    def save_retrospective(
        self, value: RetrospectiveInput, session_id: str
    ) -> RetrospectiveSnapshot: ...

    def export_retrospective(self) -> str: ...


class KubeLabApplicationService:
    """Adapter that keeps FastAPI unaware of persistence and Kubernetes clients."""

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self._runtime = runtime
        self._manager: LabManager = runtime.manager

    def close(self) -> None:
        self._runtime.close()

    def environment(self) -> EnvironmentResponse:
        return EnvironmentResponse(
            process_platform=platform.system(),
            wsl_distribution=os.environ.get("WSL_DISTRO_NAME"),
        )

    def onboarding(self) -> OnboardingState:
        if self._runtime.readiness is None:
            raise RuntimeEnvironmentError("Environment readiness service is unavailable.")
        return self._runtime.readiness.cached()

    def check_environment(self) -> EnvironmentReadinessReport:
        if self._runtime.readiness is None:
            raise RuntimeEnvironmentError("Environment readiness service is unavailable.")
        return self._runtime.readiness.check()

    def list_labs(
        self, *, category: str | None = None, progress: LabProgress | None = None
    ) -> LabCatalogResult:
        return self._manager.list_labs(category=category, progress=progress)

    def show_lab(self, lab_id: str) -> LabDetailResult:
        return self._manager.show_lab(lab_id)

    def active_session(self) -> SessionStatusResult:
        return self._manager.session_status_snapshot()

    def reconcile_active_session(self) -> SessionStatusResult:
        return self._manager.status()

    def timeline(self) -> SessionTimeline:
        return self._manager.timeline()

    def progress(self) -> LearningProgressReport:
        return self._manager.progress()

    def start(self, lab_id: str) -> LabSessionSnapshot:
        return self._manager.start(lab_id)

    def resources(self) -> SessionResources:
        return self._manager.resources()

    def events(self) -> SessionEvents:
        return self._manager.events()

    def logs(
        self,
        pod: str,
        *,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 200,
    ) -> LogResult:
        return self._manager.logs(
            pod,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
        )

    def verify(self) -> ValidationRunResult:
        return self._manager.verify()

    def next_hint(self) -> HintResult:
        return self._manager.next_hint()

    def reset(self, session_id: str) -> LabSessionSnapshot:
        return self._manager.reset(session_id)

    def cleanup(self, session_id: str) -> LabSessionSnapshot:
        return self._manager.cleanup(session_id)

    def retrospective(self) -> RetrospectiveEditState:
        return self._manager.retrospective()

    def save_retrospective(
        self, value: RetrospectiveInput, session_id: str
    ) -> RetrospectiveSnapshot:
        return self._manager.save_retrospective(value, session_id)

    def export_retrospective(self) -> str:
        return self._manager.export_retrospective()


def build_web_application_service() -> WebApplicationService:  # pragma: no cover
    """Build the production service; called once by the FastAPI lifespan."""
    return KubeLabApplicationService(build_application_runtime())


def create_app(
    service_factory: Callable[[], WebApplicationService] = build_web_application_service,
) -> FastAPI:
    """Create a local-only API application with an injectable application service."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = service_factory()
        app.state.application_service = service
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="KubeLab local API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def local_request_security(
        request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        origin = request.headers.get("origin")
        if origin is not None and origin != WEB_ORIGIN:
            return _secure_response(
                _error_response(
                    "ORIGIN_REJECTED",
                    "The request Origin is not allowed.",
                    status_code=403,
                    request_id=request_id,
                ),
                request_id=request_id,
            )
        if request.method not in _SAFE_METHODS:
            if origin != WEB_ORIGIN:
                return _secure_response(
                    _error_response(
                        "ORIGIN_REQUIRED",
                        "A same-origin request is required.",
                        status_code=403,
                        request_id=request_id,
                    ),
                    request_id=request_id,
                )
            cookie_token = request.cookies.get(CSRF_COOKIE)
            header_token = request.headers.get(CSRF_HEADER)
            if (
                not cookie_token
                or not header_token
                or not hmac.compare_digest(cookie_token, header_token)
            ):
                return _secure_response(
                    _error_response(
                        "CSRF_TOKEN_INVALID",
                        "A valid CSRF token is required.",
                        status_code=403,
                        request_id=request_id,
                    ),
                    request_id=request_id,
                )

        response = cast(Response, await call_next(request))
        csrf_token: str | None = None
        set_csrf_cookie = False
        if request.method in _SAFE_METHODS:
            csrf_token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
            response.headers[CSRF_HEADER] = csrf_token
            set_csrf_cookie = not bool(request.cookies.get(CSRF_COOKIE))
        if set_csrf_cookie and csrf_token is not None:
            response.set_cookie(
                CSRF_COOKIE,
                csrf_token,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return _secure_response(response, request_id=request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        locations = [".".join(str(part) for part in item["loc"]) for item in error.errors()]
        return _error_response(
            "REQUEST_VALIDATION_FAILED",
            "The request did not satisfy the API schema.",
            context={"field": ",".join(locations)},
            status_code=422,
            request_id=_request_id(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "RESOURCE_NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        message = (
            "The requested API resource was not found."
            if error.status_code == 404
            else "The request failed."
        )
        return _error_response(
            code,
            message,
            status_code=error.status_code,
            request_id=_request_id(request),
        )

    @app.exception_handler(Exception)
    async def application_error_handler(request: Request, error: Exception) -> JSONResponse:
        status_code, code, message, context, retryable = _public_error(error)
        return _error_response(
            code,
            message,
            context=context,
            retryable=retryable,
            status_code=status_code,
            request_id=_request_id(request),
        )

    def render_page(
        request: Request,
        template_name: str,
        *,
        title: str,
        page: str,
        lab_id: str | None = None,
        session_id: str | None = None,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            template_name,
            {
                "title": title,
                "page": page,
                "lab_id": lab_id,
                "session_id": session_id,
                "version": __version__,
            },
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page(request: Request) -> Response:
        return render_page(request, "dashboard.html", title="总览", page="dashboard")

    @app.get("/labs", response_class=HTMLResponse, include_in_schema=False)
    def labs_page(request: Request) -> Response:
        return render_page(request, "labs.html", title="实验目录", page="labs")

    @app.get("/onboarding", response_class=HTMLResponse, include_in_schema=False)
    def onboarding_page(request: Request) -> Response:
        return render_page(request, "onboarding.html", title="环境引导", page="onboarding")

    @app.get("/labs/{lab_id}", response_class=HTMLResponse, include_in_schema=False)
    def lab_page(request: Request, lab_id: str) -> Response:
        return render_page(
            request,
            "lab_detail.html",
            title="实验详情",
            page="lab-detail",
            lab_id=lab_id,
        )

    @app.get(
        "/sessions/{session_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def session_page(request: Request, session_id: str) -> Response:
        return render_page(
            request,
            "session.html",
            title="排障工作台",
            page="session",
            session_id=session_id,
        )

    @app.get("/progress", response_class=HTMLResponse, include_in_schema=False)
    def progress_page(request: Request) -> Response:
        return render_page(request, "progress.html", title="学习进度", page="progress")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    @app.get("/api/v1/environment", response_model=EnvironmentResponse)
    def environment(request: Request) -> EnvironmentResponse:
        return _service(request).environment()

    @app.get("/api/v1/onboarding", response_model=OnboardingState)
    def onboarding(request: Request) -> OnboardingState:
        return _service(request).onboarding()

    @app.post("/api/v1/onboarding/check", response_model=EnvironmentReadinessReport)
    def check_environment(request: Request) -> EnvironmentReadinessReport:
        return _service(request).check_environment()

    @app.get("/api/v1/labs", response_model=LabsResponse)
    def labs(
        request: Request,
        category: str | None = None,
        progress: LabProgress | None = None,
    ) -> LabsResponse:
        result = _service(request).list_labs(category=category, progress=progress)
        return LabsResponse(labs=result.labs, invalid_lab_count=len(result.errors))

    @app.get("/api/v1/progress", response_model=LearningProgressReport)
    def progress(request: Request) -> LearningProgressReport:
        return _service(request).progress()

    @app.get("/api/v1/labs/{lab_id}", response_model=LabDetailResult)
    def lab_detail(request: Request, lab_id: str) -> LabDetailResult:
        return _service(request).show_lab(lab_id)

    @app.get("/api/v1/sessions/active", response_model=ActiveSessionResponse)
    def active_session(request: Request) -> ActiveSessionResponse:
        result = _service(request).active_session()
        return _active_session_response(result)

    @app.post("/api/v1/sessions/active/reconcile", response_model=ActiveSessionResponse)
    def reconcile_active_session(request: Request) -> ActiveSessionResponse:
        return _active_session_response(_service(request).reconcile_active_session())

    @app.get("/api/v1/sessions/active/timeline", response_model=SessionTimeline)
    def timeline(request: Request) -> SessionTimeline:
        return _service(request).timeline()

    @app.post("/api/v1/labs/{lab_id}/start", response_model=PublicSession, status_code=201)
    def start(request: Request, lab_id: str) -> PublicSession:
        return _public_session(_service(request).start(lab_id))

    @app.get("/api/v1/sessions/active/resources", response_model=ResourcesResponse)
    def resources(request: Request) -> ResourcesResponse:
        result = _service(request).resources()
        return ResourcesResponse(
            session=_public_session(result.session),
            resources=tuple(
                PublicResourceSummary(
                    kind=item.kind,
                    name=item.name,
                    status=_redacted_optional_text(item.status),
                )
                for item in result.resources
                if item.kind in _PUBLIC_RESOURCE_KINDS
            ),
            pods=tuple(
                PublicPodSummary(
                    name=item.name,
                    phase=item.phase,
                    ready=item.ready,
                    restart_count=item.restart_count,
                    reason=_redacted_optional_text(item.reason),
                )
                for item in result.pods
            ),
        )

    @app.get("/api/v1/sessions/active/events", response_model=EventsResponse)
    def events(request: Request) -> EventsResponse:
        result = _service(request).events()
        return EventsResponse(
            session=_public_session(result.session),
            events=tuple(
                event.model_copy(update={"message": _redacted_optional_text(event.message)})
                for event in result.events
            ),
        )

    @app.get("/api/v1/sessions/active/logs", response_model=LogResult)
    def logs(
        request: Request,
        pod: str = Query(pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", max_length=253),
        container: str | None = Query(
            default=None,
            pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$",
            max_length=253,
        ),
        previous: bool = False,
        tail: int = Query(default=200, ge=1, le=500),
    ) -> LogResult:
        result = _service(request).logs(
            pod, container=container, previous=previous, tail_lines=tail
        )
        return result.model_copy(update={"content": _redacted_text(result.content)})

    @app.post("/api/v1/sessions/active/verify", response_model=VerificationResponse)
    def verify(request: Request) -> VerificationResponse:
        return _verification_response(_service(request).verify())

    @app.post("/api/v1/sessions/active/hint", response_model=HintResult)
    def hint(request: Request) -> HintResult:
        result = _service(request).next_hint()
        return result.model_copy(update={"content": _redacted_text(result.content)})

    @app.post("/api/v1/sessions/active/reset", response_model=PublicSession)
    def reset(request: Request, confirmation: ConfirmationRequest) -> PublicSession:
        service = _service(request)
        active = service.active_session().session
        _require_namespace_confirmation(active, confirmation)
        return _public_session(service.reset(active.id))

    @app.post("/api/v1/sessions/active/cleanup", response_model=PublicSession)
    def cleanup(request: Request, confirmation: ConfirmationRequest) -> PublicSession:
        service = _service(request)
        active = service.active_session().session
        _require_namespace_confirmation(active, confirmation)
        return _public_session(service.cleanup(active.id))

    @app.get(
        "/api/v1/sessions/latest/retrospective",
        response_model=RetrospectiveResponse,
    )
    def retrospective(request: Request) -> RetrospectiveResponse:
        state = _service(request).retrospective()
        return RetrospectiveResponse(
            session=_public_session(state.session),
            retrospective=_public_retrospective(state.retrospective),
            metadata=state.metadata,
        )

    @app.get("/api/v1/sessions/latest/retrospective/export")
    def export_retrospective(request: Request) -> Response:
        return Response(
            content=_service(request).export_retrospective(),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="kubelab-retrospective.md"'},
        )

    @app.put(
        "/api/v1/sessions/latest/retrospective",
        response_model=RetrospectiveSnapshot,
    )
    def save_retrospective(request: Request, value: RetrospectiveInput) -> RetrospectiveSnapshot:
        service = _service(request)
        state = service.retrospective()
        saved = _public_retrospective(service.save_retrospective(value, state.session.id))
        assert saved is not None
        return saved

    return app


def _service(request: Request) -> WebApplicationService:
    return cast(WebApplicationService, request.app.state.application_service)


def _public_session(session: LabSessionSnapshot) -> PublicSession:
    return PublicSession(
        id=session.id,
        lab_id=session.lab_id,
        namespace=session.namespace,
        status=session.status,
        created_at=session.created_at,
        started_at=session.started_at,
        completed_at=session.completed_at,
        reset_count=session.reset_count,
        last_error_code=session.last_error_code,
    )


def _active_session_response(result: SessionStatusResult) -> ActiveSessionResponse:
    return ActiveSessionResponse(
        session=_public_session(result.session),
        namespace_exists=result.namespace_exists,
        namespace_owned=result.namespace_owned,
        cluster_state=result.cluster_state.value,
        stage=result.stage.value,
        workspace_command=result.workspace_command,
    )


def _verification_response(result: ValidationRunResult) -> VerificationResponse:
    return VerificationResponse(
        id=result.id,
        session_id=result.session_id,
        status=public_validation_outcome(result.status).value,
        checked_at=result.checked_at,
        duration_ms=result.duration_ms,
        results=tuple(
            PublicVerificationCheck(
                check_id=check.check_id,
                check_type=check.check_type,
                status=public_validation_outcome(check.status).value,
                message=_redacted_text(check.message),
                retryable=check.retryable,
                duration_ms=check.duration_ms,
            )
            for check in result.results
        ),
    )


def _public_retrospective(
    value: RetrospectiveSnapshot | None,
) -> RetrospectiveSnapshot | None:
    if value is None:
        return None
    return RetrospectiveSnapshot(
        session_id=value.session_id,
        symptom=_redacted_text(value.symptom),
        impact=_redacted_text(value.impact),
        investigation=_redacted_text(value.investigation),
        root_cause=_redacted_text(value.root_cause),
        resolution=_redacted_text(value.resolution),
        prevention=_redacted_text(value.prevention),
        interview_summary=_redacted_text(value.interview_summary),
        updated_at=value.updated_at,
    )


def _redacted_optional_text(value: str | None) -> str | None:
    return _redacted_text(value) if value is not None else None


def _redacted_text(value: str) -> str:
    safe = str(redact_json(value)).replace("\r", " ").replace("\n", " ")[:500]
    lowered = safe.casefold()
    if "traceback" in lowered or ("apiversion:" in lowered and "kind:" in lowered):
        return "Details unavailable."
    return safe


def _require_namespace_confirmation(
    session: LabSessionSnapshot, confirmation: ConfirmationRequest
) -> None:
    if not hmac.compare_digest(session.namespace, confirmation.namespace):
        raise LabManagerError(
            "NAMESPACE_CONFIRMATION_MISMATCH",
            "The Namespace confirmation does not match the active Session.",
            context={"namespace": session.namespace},
        )


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def _error_response(
    code: str,
    message: str,
    *,
    context: dict[str, JsonValue] | None = None,
    retryable: bool = False,
    status_code: int,
    request_id: str,
) -> JSONResponse:
    payload = ErrorResponse(
        code=code,
        message=message,
        context=context or {},
        retryable=retryable,
    )
    response = JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def _secure_response(response: Response, *, request_id: str) -> Response:
    response.headers[REQUEST_ID_HEADER] = request_id
    response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


def _public_error(
    error: Exception,
) -> tuple[int, str, str, dict[str, JsonValue], bool]:
    code = str(getattr(error, "code", "INTERNAL_ERROR"))
    retryable = bool(getattr(error, "retryable", False))
    raw_context = getattr(error, "context", {})
    context = _safe_context(raw_context)
    if isinstance(error, ActiveSessionConflict) and error.active is not None:
        context = {"session_id": error.active.id, "status": error.active.status.value}
    if isinstance(
        error,
        (LabManagerError, EnvironmentNotReadyError, ContextError, ConfigError, DatabaseError),
    ):
        message = _redacted_text(str(getattr(error, "message", str(error))))
    elif isinstance(error, (ActiveSessionConflict, OperationLockError, RuntimeEnvironmentError)):
        message = _redacted_text(str(error))
    else:
        return 500, "INTERNAL_ERROR", "KubeLab could not complete the request.", {}, False
    return _status_for_error(code), code, message, context, retryable


def _safe_context(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, JsonValue] = {}
    for key, item in value.items():
        if key not in _PUBLIC_CONTEXT_KEYS or not isinstance(item, (str, int, float, bool)):
            continue
        safe[key] = item
    return safe


def _status_for_error(code: str) -> int:
    if code in {"LAB_NOT_FOUND", "SESSION_NOT_FOUND", "KUBERNETES_NOT_FOUND"}:
        return 404
    if code in {
        "CONTEXT_DRIFT",
        "CONTEXT_FINGERPRINT_MISMATCH",
        "CONTEXT_NOT_LOCAL_MINIKUBE",
        "CONTEXT_NOT_TRUSTED",
        "KUBERNETES_FORBIDDEN",
        "KUBERNETES_UNAUTHORIZED",
        "NAMESPACE_OWNERSHIP_MISMATCH",
        "SESSION_SCOPE_INVALID",
    }:
        return 403
    if code in {
        "ACTIVE_SESSION_CONFLICT",
        "INVALID_SESSION_STATE",
        "NAMESPACE_CONFIRMATION_MISMATCH",
        "OPERATION_IN_PROGRESS",
    }:
        return 409
    if code in {"LAB_INVALID"}:
        return 422
    if code in {
        "CLUSTER_OPERATION_FAILED",
        "CLEANUP_FAILED",
        "DATABASE_ERROR",
        "KUBERNETES_API_ERROR",
        "KUBERNETES_TIMEOUT",
        "RUNTIME_PLATFORM_UNSUPPORTED",
        "ENVIRONMENT_NOT_READY",
    }:
        return 503
    return 500


__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "EnvironmentResponse",
    "KubeLabApplicationService",
    "WEB_HOST",
    "WEB_ORIGIN",
    "WEB_PORT",
    "WebApplicationService",
    "build_web_application_service",
    "create_app",
]
