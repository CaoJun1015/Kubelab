"""Credential redaction tests for persisted JSON and text fields."""

from __future__ import annotations

from kubelab.redaction import REDACTED, redact_json


def test_sensitive_keys_are_redacted_recursively() -> None:
    value = {
        "token": "top-secret",
        "nested": {
            "client-key": "private",
            "certificateData": "cert",
            "password": "password",
        },
        "items": [{"authorization": "Basic abc"}, {"safe": "visible"}],
    }

    redacted = redact_json(value)

    assert redacted == {
        "token": REDACTED,
        "nested": {
            "client-key": REDACTED,
            "certificateData": REDACTED,
            "password": REDACTED,
        },
        "items": [{"authorization": REDACTED}, {"safe": "visible"}],
    }
    assert value["token"] == "top-secret"


def test_bearer_query_credentials_and_pem_are_removed() -> None:
    value = {
        "message": "request Bearer abc.def-123 failed",
        "url": "https://example.test/?token=secret&safe=yes",
        "pem": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
    }

    redacted = redact_json(value)

    assert redacted["message"] == "request Bearer [REDACTED] failed"
    assert redacted["url"] == "https://example.test/?token=[REDACTED]&safe=yes"
    assert redacted["pem"] == REDACTED


def test_large_strings_are_truncated_and_non_json_values_become_strings() -> None:
    marker = object()
    redacted = redact_json({"large": "x" * 5000, "marker": marker, "number": 2})

    assert redacted["large"].endswith("...[TRUNCATED]")
    assert len(redacted["large"]) < 5000
    assert redacted["marker"] == str(marker)
    assert redacted["number"] == 2
