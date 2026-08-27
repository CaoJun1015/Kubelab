"""Server-rendered Web UI and frontend security contract tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from kubelab.web import CSRF_COOKIE, CSRF_HEADER, create_app


@pytest.fixture
def ui_client():
    service = SimpleNamespace(close=lambda: None)
    with TestClient(create_app(lambda: service)) as client:  # type: ignore[arg-type]
        yield client


@pytest.mark.parametrize(
    ("path", "page", "heading"),
    [
        ("/", "dashboard", "今天，从一个真实故障开始。"),
        ("/labs", "labs", "实验目录"),
        ("/labs/lab-005-image-pull", "lab-detail", "你的任务"),
        ("/sessions/123e4567-e89b-42d3-a456-426614174111", "session", "资源与 Pods"),
        ("/progress", "progress", "学习进度"),
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
    assert 'href="/progress"' in response.text
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
    assert 'document.addEventListener("visibilitychange"' in script
    assert 'document.querySelector("#refresh-events").addEventListener("click"' in script
    assert 'document.querySelector("#refresh-logs").addEventListener("click"' in script
    assert "input.value !== state.activeSession.namespace" in script
    assert 'button.dataset.busy === "true"' in script
    assert "navigator.clipboard.writeText" in script
    assert "kubelab workspace enter" in script
    assert "expected" not in script
    assert "actual" not in script


def test_session_mismatch_is_a_public_non_retryable_ui_error() -> None:
    script = (Path(__file__).parents[1] / "src" / "kubelab" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'code: "SESSION_ID_MISMATCH"' in script
    assert "active.session.id !== routeSessionId" in script
    assert "当前活动 Session 与页面地址不一致" in script


def test_built_wheel_contains_templates_and_static_assets(tmp_path: Path) -> None:
    project = Path(__file__).parents[1]
    output = tmp_path / "wheel"
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
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
    } <= files
    lab_definitions = {
        name for name in files if name.startswith("kubelab/labs/") and name.endswith("/lab.yaml")
    }
    assert len(lab_definitions) == 12
    assert "kubelab/labs/lab-012-pvc-pending/lab.yaml" in lab_definitions
