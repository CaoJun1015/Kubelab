"""Release script contract tests that do not access a real cluster."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from xml.etree.ElementTree import Element, ElementTree, SubElement

import pytest


def load_smoke_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "validate_release_smoke.py"
    spec = importlib.util.spec_from_file_location("validate_release_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_acceptance_validator() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "validate_integration_acceptance.py"
    spec = importlib.util.spec_from_file_location("validate_integration_acceptance", path)
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


def test_acceptance_junit_requires_exact_all_pass_count(tmp_path: Path) -> None:
    validator = load_acceptance_validator()
    report = tmp_path / "batch.xml"
    suite = Element("testsuite")
    for number in range(6):
        SubElement(suite, "testcase", name=f"scenario-{number}")
    ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)

    assert validator.validate_junit(report, 6) == {
        "tests": 6,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    SubElement(suite[-1], "skipped")
    ElementTree(suite).write(report, encoding="utf-8", xml_declaration=True)
    with pytest.raises(ValueError, match="exact all-pass"):
        validator.validate_junit(report, 6)


def test_acceptance_profile_and_context_fail_closed() -> None:
    validator = load_acceptance_validator()
    profile = {"valid": [{"Name": "minikube", "Status": "Stopped", "Config": {"Driver": "docker"}}]}
    context = {
        "context_name": "minikube",
        "minikube_profile": "minikube",
        "trust_state": "trusted",
        "trusted": True,
    }

    assert validator.validate_profile(profile) == ("Stopped", "docker")
    assert validator.validate_context(context) is None
    context["trust_state"] = "drifted"
    with pytest.raises(ValueError, match="not already trusted"):
        validator.validate_context(context)


def test_acceptance_residue_audit_detects_managed_resources_and_temp_paths(
    tmp_path: Path,
) -> None:
    validator = load_acceptance_validator()
    temporary = tmp_path / "kubelab-workspace-private"
    residue = validator.residue_from_reports(
        {
            "items": [
                {
                    "kind": "Namespace",
                    "metadata": {
                        "name": "kubelab-test-one",
                        "labels": {"kubelab.io/managed-by": "kubelab"},
                    },
                }
            ]
        },
        {
            "items": [
                {
                    "kind": "RoleBinding",
                    "metadata": {"name": "kubelab-workspace", "namespace": "leftover"},
                },
                {
                    "kind": "Pod",
                    "metadata": {"name": "kubelab-probe-dns", "namespace": "leftover"},
                },
            ]
        },
        {
            "items": [
                {
                    "kind": "PersistentVolume",
                    "metadata": {"name": "pvc-one"},
                    "spec": {"claimRef": {"namespace": "kubelab-test-one"}},
                }
            ]
        },
        (temporary,),
    )

    assert residue == (
        "Namespace/kubelab-test-one",
        "PersistentVolume/pvc-one",
        "Pod/leftover/kubelab-probe-dns",
        "RoleBinding/leftover/kubelab-workspace",
        "TemporaryPath/kubelab-workspace-private",
    )


def test_wsl_acceptance_runner_uses_fixed_batches_and_never_mutates_trust() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "wsl_m6_1_acceptance.sh").read_text(
        encoding="utf-8"
    )

    for batch in (
        "baseline-001-012",
        "baseline-013-021",
        "variants-013-015",
        "variants-016-018",
    ):
        assert batch in script
    assert "KUBELAB_LAB_INTEGRATION_BATCH" in script
    assert "minikube stop --profile minikube" in script
    assert '"$validator" audit' in script
    assert 'uv venv "$results_root/venv" --python 3.11' in script
    assert "uv sync --active --locked --dev" in script
    assert "uv run" not in script
    assert "context trust" not in script
    assert "kubectl delete" not in script


def test_wsl_quality_gate_is_isolated_and_keeps_real_integration_disabled() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "wsl_m6_1_quality.sh").read_text(
        encoding="utf-8"
    )

    assert 'mktemp -d "/tmp/kubelab-m6-1-quality.' in script
    assert "uv venv" in script
    assert "--python 3.11" in script
    assert "KUBELAB_RUN_INTEGRATION=0" in script
    assert "KUBELAB_RUN_LAB_INTEGRATION=0" in script
    assert "python -m pytest" in script
    assert "0.3.0rc1" in script
