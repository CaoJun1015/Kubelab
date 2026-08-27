"""Defensive redaction for structured values written to local persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "certificate",
    "client_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_PEM_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:access_token|api_key|password|secret|token)=)[^&#\s]+"
)
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(?<![?&])\b(authorization|credential|password|private[_-]?key|secret|token)"
    r"\s*[:=]\s*[^\s,;]+"
)
_MAX_STRING_LENGTH = 4096


def redact_json(value: Any) -> Any:
    """Return a JSON-compatible deep copy with common credentials removed."""
    return _redact(value, key=None)


def _redact(value: Any, *, key: str | None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(child, key=str(child_key)) for child_key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(child, key=None) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> str:
    if _PEM_PATTERN.search(value):
        return REDACTED
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _QUERY_SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _KEY_VALUE_SECRET_PATTERN.sub(r"\1=[REDACTED]", redacted)
    if len(redacted) > _MAX_STRING_LENGTH:
        return redacted[:_MAX_STRING_LENGTH] + "...[TRUNCATED]"
    return redacted


__all__ = ["REDACTED", "redact_json"]
