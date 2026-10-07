"""Server-rendered Web UI and frontend security contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from kubelab.web import CSRF_COOKIE, CSRF_HEADER, create_app


class ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def by_id(self, element_id: str) -> tuple[str, dict[str, str | None]]:
        return next(element for element in self.elements if element[1].get("id") == element_id)


@pytest.fixture
def ui_client():
    service = SimpleNamespace(close=lambda: None)
    with TestClient(create_app(lambda: service)) as client:  # type: ignore[arg-type]
        yield client


@pytest.mark.parametrize(
    ("path", "page", "heading"),
    [
        ("/", "dashboard", "今天，从一个真实故障开始。"),
        ("/onboarding", "onboarding", "准备本地实验环境"),
        ("/labs", "labs", "实验目录"),
        ("/paths", "paths", "专题学习路径"),
        ("/paths/service-discovery-traffic", "path-detail", "能力地图"),
        ("/paths/service-discovery-traffic/outcome", "path-outcome", "专题成果"),
        ("/symptoms", "symptoms", "从症状开始排障"),
        ("/labs/lab-005-image-pull", "lab-detail", "你的任务"),
        ("/sessions/123e4567-e89b-42d3-a456-426614174111", "session", "资源状态"),
        ("/progress", "progress", "学习进度"),
        ("/packages", "packages", "本地实验包"),
    ],
)
def test_page_shells_render_navigation_and_expected_landmarks(
    ui_client: TestClient, path: str, page: str, heading: str
) -> None:
    response = ui_client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f'data-page="{page}"' in response.text
    assert heading in response.text
    assert 'href="/"' in response.text
    assert 'href="/labs"' in response.text
    assert 'href="/paths"' in response.text
    assert 'href="/onboarding"' in response.text
    assert 'href="/progress"' in response.text
    assert 'href="/packages"' in response.text
    assert 'src="http://testserver/static/app.js"' in response.text


def test_page_route_values_are_jinja_escaped(ui_client: TestClient) -> None:
    attack = '<img src=x onerror="alert(1)">'
    response = ui_client.get(f"/labs/{quote(attack, safe='')}")

    assert response.status_code == 200
    assert attack not in response.text
    assert "&lt;img" in response.text
    assert "onerror=&#34;alert(1)&#34;" in response.text


def test_html_and_static_assets_receive_strict_security_headers(ui_client: TestClient) -> None:
    for path in ("/", "/static/app.js", "/health"):
        response = ui_client.get(path)
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in response.headers


def test_safe_requests_always_echo_current_csrf_token(ui_client: TestClient) -> None:
    first = ui_client.get("/")
    token = first.headers[CSRF_HEADER]
    second = ui_client.get("/health")

    assert token
    assert second.headers[CSRF_HEADER] == token
    assert CSRF_COOKIE in ui_client.cookies
    assert "set-cookie" not in second.headers


def test_frontend_uses_text_only_rendering_and_required_interaction_guards() -> None:
    script = (Path(__file__).parents[1] / "src" / "kubelab" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert ".innerHTML" not in script
    assert ".outerHTML" not in script
    assert "textContent" in script
    assert 'error.code === "CSRF_TOKEN_INVALID"' in script
    assert "return api(path, options, false)" in script
    assert "window.setInterval(pollResources, 2000)" in script
    assert 'button.setAttribute("aria-busy", "true")' in script
    assert 'button.removeAttribute("aria-busy")' in script
    assert 'text("#poll-status", "正在刷新…")' in script
    assert 'text("#poll-status", "刷新失败")' in script
    assert 'document.querySelectorAll("#resources-table, #pods-table")' in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert 'document.querySelector("#refresh-events").addEventListener("click"' in script
    assert 'document.querySelector("#refresh-logs").addEventListener("click"' in script
    assert "input.value !== state.activeSession.namespace" in script
    assert 'button.dataset.busy === "true"' in script
    assert "navigator.clipboard.writeText" in script
    assert "kubelab workspace enter" in script
    assert "/api/v1/sessions/active/reconcile" in script
    assert "/api/v1/sessions/active/timeline" in script
    assert "/api/v1/progress" in script
    assert "/api/v1/learning-paths" in script
    assert "/api/v1/symptoms" in script
    assert "/api/v1/packages" in script
    assert 'detail.lab.source === "local_package"' in script
    assert "未验证（自声明信息）" in script
    assert "expected" not in script
    assert "actual" not in script


def test_web_ui_accessibility_and_filter_url_contracts() -> None:
    project = Path(__file__).parents[1]
    script = (project / "src" / "kubelab" / "static" / "app.js").read_text(encoding="utf-8")
    session = (project / "src" / "kubelab" / "templates" / "session.html").read_text(
        encoding="utf-8"
    )
    labs = (project / "src" / "kubelab" / "templates" / "labs.html").read_text(encoding="utf-8")
    detail = (project / "src" / "kubelab" / "templates" / "lab_detail.html").read_text(
        encoding="utf-8"
    )

    assert 'id="start-lab"' in detail and 'disabled aria-busy="true"' in detail
    assert "正在读取实验…" in detail
    assert "正在恢复 Session…" in session
    assert 'class="session-workspace"' in session
    assert 'class="workspace-rail"' in session
    assert 'class="workspace-guidance"' in session
    assert 'role="alert"' in session
    assert session.count('disabled aria-busy="true"') == 9
    assert 'id="poll-status" class="quiet-text" role="status" aria-live="polite"' in session
    assert (
        'id="confirmation-dialog" class="confirmation-dialog" '
        'aria-labelledby="confirmation-title" aria-describedby="confirmation-copy"' in session
    )
    assert session.count('scope="col"') == 8
    assert 'id="resources-table"' in session and 'id="pods-table"' in session
    assert 'value="">全部分类' in labs and 'value="">全部进度' in labs
    assert 'filters.get("category")' in script
    assert 'filters.get("progress")' in script
    assert "url.searchParams.set(name, value)" in script
    assert "window.history.replaceState" in script
    assert "input.focus();" in script
    assert "confirmationTrigger?.focus();" in script
    assert "finally { trigger.focus(); }" in script
    assert "button.disabled = false;" in script
    assert "document.querySelectorAll(sessionActionSelector)" in script
    assert "document.querySelectorAll(\"button[aria-busy='true']\")" in script
    assert "实验读取失败，请刷新页面重试。" in script
    assert "Session 恢复失败，请刷新页面重试。" in script
    assert 'text("#session-next-step-title", guidance[0])' in script
    assert 'text("#session-completion", detail.completion_description)' in script


def test_rendered_session_dom_has_initialization_and_dialog_guards(
    ui_client: TestClient,
) -> None:
    response = ui_client.get("/sessions/123e4567-e89b-42d3-a456-426614174111")
    parser = ElementCollector()
    parser.feed(response.text)

    action_ids = {
        "copy-namespace",
        "reconcile-session",
        "refresh-events",
        "refresh-logs",
        "run-verify",
        "request-hint",
        "reset-session",
        "cleanup-session",
    }
    for action_id in action_ids:
        tag, attrs = parser.by_id(action_id)
        assert tag == "button"
        assert "disabled" in attrs
        assert attrs["aria-busy"] == "true"

    dialog_tag, dialog = parser.by_id("confirmation-dialog")
    assert dialog_tag == "dialog"
    assert dialog["aria-labelledby"] == "confirmation-title"
    assert dialog["aria-describedby"] == "confirmation-copy"
    poll_status = parser.by_id("poll-status")[1]
    assert poll_status["role"] == "status"
    assert poll_status["aria-live"] == "polite"
    for table_id in ("resources-table", "pods-table"):
        assert parser.by_id(table_id)[0] == "table"
    table_headers = [attrs for tag, attrs in parser.elements if tag == "th"]
    assert len(table_headers) == 8
    assert all(attrs.get("scope") == "col" for attrs in table_headers)


def test_redesign_css_is_merged_and_keeps_guidance_before_long_content() -> None:
    stylesheet = (
        Path(__file__).parents[1] / "src" / "kubelab" / "static" / "styles.css"
    ).read_text(encoding="utf-8")

    assert stylesheet.count(":root {") == 1
    medium_layout = stylesheet.split("@media (max-width: 1220px)", maxsplit=1)[1]
    assert "grid-template-columns: 1fr;" in medium_layout
    assert ".workspace-primary {\n    order: 3;" in medium_layout
    assert "order: 2;" in medium_layout


def test_frontend_initialization_failure_runtime_contract() -> None:
    project = Path(__file__).parents[1]
    subprocess.run(
        ["node", "--test", "tests/web_ui_runtime.test.cjs"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_session_mismatch_is_a_public_non_retryable_ui_error() -> None:
    script = (Path(__file__).parents[1] / "src" / "kubelab" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'code: "SESSION_ID_MISMATCH"' in script
    assert "active.session.id !== routeSessionId" in script
    assert "当前活动 Session 与页面地址不一致" in script


def test_built_distributions_pass_shared_release_verifier(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    output = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--cache-dir",
            str(project / ".uv-cache"),
            "--out-dir",
            str(output),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=os.environ.copy(),
    )
    wheel = next(output.glob("kubelab-*.whl"))
    subprocess.run(
        [
            sys.executable,
            str(project / "scripts" / "verify_distribution.py"),
            "--dist-dir",
            str(output),
            "--project-file",
            str(project / "pyproject.toml"),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with ZipFile(wheel) as archive:
        files = set(archive.namelist())

    assert {
        "kubelab/static/app.js",
        "kubelab/static/styles.css",
        "kubelab/templates/base.html",
        "kubelab/templates/dashboard.html",
        "kubelab/templates/labs.html",
        "kubelab/templates/lab_detail.html",
        "kubelab/templates/session.html",
        "kubelab/templates/progress.html",
        "kubelab/templates/onboarding.html",
        "kubelab/templates/packages.html",
        "kubelab/templates/paths.html",
        "kubelab/templates/path_detail.html",
        "kubelab/templates/path_outcome.html",
        "kubelab/templates/symptoms.html",
        "kubelab/content/learning-paths.yaml",
    } <= files
    lab_definitions = {
        name for name in files if name.startswith("kubelab/labs/") and name.endswith("/lab.yaml")
    }
    assert len(lab_definitions) == 21
    variant_definitions = {
        name
        for name in files
        if name.startswith("kubelab/labs/") and name.endswith("/variant.yaml")
    }
    assert len(variant_definitions) == 12
    assert "kubelab/labs/lab-018-pvc-claim-missing/lab.yaml" in lab_definitions
