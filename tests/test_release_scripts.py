"""Release script contract tests that do not access a real cluster."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def load_smoke_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "validate_release_smoke.py"
    spec = importlib.util.spec_from_file_location("validate_release_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog() -> dict[str, object]:
    return {
        "labs": [
            {"id": f"lab-{number:03d}", "variant_total": 2 if 13 <= number <= 18 else 0}
            for number in range(1, 22)
        ],
        "errors": [],
    }


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("healthy", 0), ("degraded", 0), ("unhealthy", 3)],
)
def test_release_smoke_accepts_valid_doctor_outcomes(status: str, exit_code: int) -> None:
    validator = load_smoke_validator()

    assert validator.validate_reports({"status": status}, catalog(), exit_code) == (
        status,
        21,
        12,
        33,
    )


@pytest.mark.parametrize(
    ("doctor", "exit_code"),
    [({"status": "healthy"}, 3), ({"status": "unhealthy"}, 0), ({"status": "healthy"}, 2)],
)
def test_release_smoke_rejects_inconsistent_doctor_outcome(
    doctor: dict[str, str], exit_code: int
) -> None:
    validator = load_smoke_validator()

    with pytest.raises(ValueError, match="Doctor"):
        validator.validate_reports(doctor, catalog(), exit_code)


def test_release_smoke_rejects_wrong_or_invalid_registry_counts() -> None:
    validator = load_smoke_validator()
    wrong_count = catalog()
    labs = wrong_count["labs"]
    assert isinstance(labs, list)
    labs.pop()

    with pytest.raises(ValueError, match="21 labs and 12 variants"):
        validator.validate_reports({"status": "healthy"}, wrong_count, 0)
    with pytest.raises(ValueError, match="malformed or contains errors"):
        validator.validate_reports(
            {"status": "healthy"}, {"labs": catalog()["labs"], "errors": ["broken"]}, 0
        )


def test_wsl_smoke_keeps_four_arguments_and_environment_aware_context() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "wsl_release_smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "[[ $# -ne 4 ]]" in script
    assert 'doctor_exit" -ne 0 && "$doctor_exit" -ne 3' in script
    assert 'context_status="skipped-not-ready"' in script
    assert "validate_release_smoke.py" in script
    assert "grep -c" not in script
    assert "trap finish EXIT" in script
