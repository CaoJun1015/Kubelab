"""M8 author-toolchain contracts never require a learner database or real cluster."""

from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

import kubelab.authoring_integration as authoring_integration
from kubelab.authoring import AuthoringService
from kubelab.authoring_fake import FakeContractError
from kubelab.authoring_integration import _docker_profile, _write_junit
from kubelab.authoring_schema import LabAuthoringContract
from kubelab.cli import app
from kubelab.safe_yaml import load_all_unique
from kubelab.schema_export import render_authoring_json_schema

LABS_ROOT = Path(__file__).resolve().parents[1] / "labs"
RUNNER = CliRunner()


def test_checked_in_authoring_schema_matches_pydantic_model() -> None:
    schema = LABS_ROOT.parent / "schemas" / "lab-authoring-v1alpha1.schema.json"

    assert schema.read_text(encoding="utf-8") == render_authoring_json_schema()


def _init(
    service: AuthoringService,
    target: Path,
    *,
    scenario_type: str = "baseline",
    scenario_id: str = "lab-sample",
) -> Path:
    report = service.init(
        target,
        scenario_type=scenario_type,
        scenario_id=scenario_id,
        title="安全作者样例",
        category="workload",
        difficulty="intermediate",
        description="用于证明M8作者工具链的安全样例。",
    )
    assert report.passed, report.issues
    return target if scenario_type != "variant" else target / "variants" / scenario_id


@pytest.mark.parametrize("scenario_type", ["baseline", "composite"])
def test_init_generates_complete_lintable_and_testable_lab(
    tmp_path: Path, scenario_type: str
) -> None:
    service = AuthoringService(tmp_path)
    lab_id = f"lab-{scenario_type}-sample"
    target = tmp_path / lab_id

    _init(service, target, scenario_type=scenario_type, scenario_id=lab_id)

    assert service.lint(target).passed
    result = service.test(target)
    assert result.passed
    assert len(result.results) == 1
    expected = {
        "lab.yaml",
        "README.md",
        "authoring.yaml",
        "manifests",
        "solutions",
    }
    assert expected == {path.name for path in target.iterdir()}


def test_init_variant_inherits_parent_and_is_immediately_testable(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    parent = tmp_path / "lab-parent-sample"
    _init(service, parent, scenario_id="lab-parent-sample")

    variant = _init(
        service,
        parent,
        scenario_type="variant",
        scenario_id="variant-b",
    )

    assert variant == parent / "variants" / "variant-b"
    report = service.test(parent)
    assert report.passed
    assert {item.scenario for item in report.results} == {
        "lab-parent-sample",
        "lab-parent-sample/variant-b",
    }


def test_init_dry_run_and_conflicts_never_write_or_overwrite(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-dry-sample"
    dry = service.init(
        target,
        scenario_type="baseline",
        scenario_id="lab-dry-sample",
        title="预览",
        category="workload",
        difficulty="beginner",
        description="预览不写文件。",
        dry_run=True,
    )
    assert dry.passed
    assert not target.exists()
    assert "authoring.yaml" in dry.files

    target.mkdir()
    marker = target / "owned.txt"
    marker.write_text("keep", encoding="utf-8")
    conflict = service.init(
        target,
        scenario_type="baseline",
        scenario_id="lab-dry-sample",
        title="冲突",
        category="workload",
        difficulty="beginner",
        description="不能覆盖。",
    )
    assert not conflict.passed
    assert conflict.exit_code == 2
    assert marker.read_text(encoding="utf-8") == "keep"


def test_init_rejects_invalid_type_workspace_and_variant_parent(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    invalid_type = service.init(
        tmp_path / "lab-invalid-type",
        scenario_type="random",
        scenario_id="lab-invalid-type",
        title="非法类型",
        category="workload",
        difficulty="beginner",
        description="类型必须固定。",
    )
    assert not invalid_type.passed
    assert invalid_type.issues[0].code == "AUTHOR_SCENARIO_TYPE_INVALID"

    plain_parent = tmp_path / "plain-parent"
    plain_parent.mkdir()
    invalid_variant = service.init(
        plain_parent,
        scenario_type="variant",
        scenario_id="variant-b",
        title="无父实验",
        category="workload",
        difficulty="beginner",
        description="变体必须从合法实验族创建。",
    )
    assert not invalid_variant.passed
    assert invalid_variant.issues[0].code == "AUTHOR_VARIANT_PARENT_INVALID"

    workspace_file = tmp_path / "not-a-directory"
    workspace_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        AuthoringService(workspace_file)


def test_init_can_atomically_replace_only_an_existing_empty_directory(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-empty-sample"
    target.mkdir()

    report = service.init(
        target,
        scenario_type="baseline",
        scenario_id="lab-empty-sample",
        title="空目录",
        category="workload",
        difficulty="beginner",
        description="仅允许替换空目录。",
    )

    assert report.passed
    assert (target / "authoring.yaml").is_file()


def test_init_rejects_workspace_escape_and_symbolic_link_targets(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    escaped = service.init(
        tmp_path.parent / "lab-escaped-sample",
        scenario_type="baseline",
        scenario_id="lab-escaped-sample",
        title="越界",
        category="workload",
        difficulty="beginner",
        description="不得写到作者工作区外。",
    )
    assert not escaped.passed
    assert escaped.exit_code == 3

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows account cannot create directory symbolic links.")
    linked = service.init(
        link / "lab-linked-sample",
        scenario_type="baseline",
        scenario_id="lab-linked-sample",
        title="链接",
        category="workload",
        difficulty="beginner",
        description="不得穿过符号链接。",
    )
    assert not linked.passed
    assert linked.exit_code == 3
    assert not (real_parent / "lab-linked-sample").exists()


def test_bundled_thirty_three_scenarios_pass_unified_lint_and_fake_contracts() -> None:
    service = AuthoringService(LABS_ROOT.parent)

    lint = service.lint(LABS_ROOT)
    tested = service.test(LABS_ROOT)

    assert lint.passed
    assert tested.passed
    assert len(lint.scenarios) == len(tested.results) == 33
    assert sum(item.scenario_type == "baseline" for item in tested.results) == 18
    assert sum(item.scenario_type == "variant" for item in tested.results) == 12
    assert sum(item.scenario_type == "composite" for item in tested.results) == 3


def test_lint_rejects_missing_fake_state_and_undeclared_repair_diff(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-invalid-sample"
    _init(service, target, scenario_id="lab-invalid-sample")
    contract_path = target / "authoring.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    del contract["states"]["faulted"]["observations"]["deployment-fault-visible"]
    contract_path.write_text(
        yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    missing = service.lint(target)
    assert not missing.passed
    assert "AUTHOR_OBSERVATION_MISSING" in {item.code for item in missing.issues}

    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["states"]["faulted"]["observations"]["deployment-fault-visible"] = {
        "type": "deployment_available",
        "availableReplicas": 1,
    }
    contract_path.write_text(
        yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    repair_path = target / "solutions" / "fix.yaml"
    repair = yaml.safe_load(repair_path.read_text(encoding="utf-8"))
    repair["spec"]["template"]["spec"]["containers"][0]["image"] = "nginx:1.26-alpine"
    repair_path.write_text(yaml.safe_dump(repair, sort_keys=False), encoding="utf-8")
    diff = service.lint(target)
    assert not diff.passed
    assert "AUTHOR_REPAIR_DIFF_UNDECLARED" in {item.code for item in diff.issues}


def test_lint_and_fake_test_fail_closed_for_missing_contract_or_runner_error(
    tmp_path: Path,
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-missing-contract"
    _init(service, target, scenario_id="lab-missing-contract")
    (target / "authoring.yaml").unlink()
    missing = service.lint(target)
    assert not missing.passed
    assert missing.issues[0].code == "AUTHOR_CONTRACT_MISSING"

    healthy = tmp_path / "lab-runner-failure"
    _init(service, healthy, scenario_id="lab-runner-failure")

    class FailingRunner:
        def run(self, executable: object, contract: object) -> None:
            raise FakeContractError(
                "AUTHOR_FAKE_FORCED_FAILURE",
                "The declarative lifecycle did not match.",
                field_path="states.repaired",
            )

    failed = AuthoringService(tmp_path, fake_runner=FailingRunner()).test(healthy)  # type: ignore[arg-type]
    assert not failed.passed
    assert failed.exit_code == 4
    assert failed.issues[0].code == "AUTHOR_FAKE_FORCED_FAILURE"


def test_lint_rejects_conflicting_shared_observations_and_illegal_recreate(
    tmp_path: Path,
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-conflict-sample"
    _init(service, target, scenario_id="lab-conflict-sample")
    definition_path = target / "lab.yaml"
    definition = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    duplicate = dict(definition["successChecks"][0])
    duplicate["id"] = "deployment-repaired-copy"
    definition["successChecks"].append(duplicate)
    definition_path.write_text(
        yaml.safe_dump(definition, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    contract_path = target / "authoring.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["states"]["faulted"]["observations"]["deployment-repaired-copy"] = {
        "type": "deployment_available",
        "availableReplicas": 9,
    }
    contract["states"]["repaired"]["observations"]["deployment-repaired-copy"] = {
        "type": "deployment_available",
        "availableReplicas": 2,
    }
    contract_path.write_text(
        yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    conflict = service.lint(target)

    assert not conflict.passed
    assert "AUTHOR_OBSERVATION_CONFLICT" in {item.code for item in conflict.issues}

    definition["successChecks"] = definition["successChecks"][:-1]
    definition_path.write_text(
        yaml.safe_dump(definition, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    for state in ("faulted", "repaired"):
        del contract["states"][state]["observations"]["deployment-repaired-copy"]
    contract["repairs"]["full"]["allowedChanges"][0]["operation"] = "recreate"
    contract_path.write_text(
        yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    recreate = service.lint(target)

    assert not recreate.passed
    assert "AUTHOR_RECREATE_KIND_FORBIDDEN" in {item.code for item in recreate.issues}


def test_composite_contract_requires_both_first_repair_state_and_plan(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-incomplete-composite"
    _init(
        service,
        target,
        scenario_type="composite",
        scenario_id="lab-incomplete-composite",
    )
    contract_path = target / "authoring.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    del contract["states"]["firstRepair"]
    contract_path.write_text(
        yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = service.lint(target)

    assert not report.passed
    assert "AUTHOR_CONTRACT_INVALID" in {item.code for item in report.issues}


@pytest.mark.parametrize(
    "payload",
    [
        "Bearer abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
        "Traceback (most recent call last)",
        r"C:\Users\someone\secret.txt",
        "<script>alert(1)</script>",
        "apiVersion: v1\nkind: Pod",
    ],
)
def test_lint_rejects_public_leaks_without_echoing_payload(tmp_path: Path, payload: str) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-leak-sample"
    _init(service, target, scenario_id="lab-leak-sample")
    definition_path = target / "lab.yaml"
    definition = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
    definition["task"]["description"] = payload
    definition_path.write_text(
        yaml.safe_dump(definition, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = service.lint(target)
    serialized = report.model_dump_json(by_alias=True)

    assert not report.passed
    assert report.exit_code == 3
    assert payload not in serialized


def test_inspect_uses_shared_blind_disclosure_boundary() -> None:
    service = AuthoringService(LABS_ROOT.parent)
    target = LABS_ROOT / "lab-013-service-target-port" / "variants" / "variant-b"

    report = service.inspect(target)

    assert report.passed
    scenario = report.scenarios[0]
    assert not scenario.before_pass.revealed
    assert scenario.before_pass.scenario_name is None
    assert scenario.before_pass.root_cause is None
    assert scenario.before_pass.check_types == ()
    assert scenario.after_pass.revealed
    assert scenario.after_pass.scenario_name
    assert scenario.after_pass.root_cause
    assert all(not path.startswith("/") for path in scenario.files)


def test_package_is_deterministic_indexed_and_non_installing(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-package-sample"
    _init(service, target, scenario_id="lab-package-sample")
    first_path = tmp_path / "first.kubelab-lab.tar.gz"
    second_path = tmp_path / "second.kubelab-lab.tar.gz"

    first = service.package(target, output=first_path)
    second = service.package(target, output=second_path)

    assert first.passed and second.passed
    assert first.sha256 == second.sha256
    assert first_path.read_bytes() == second_path.read_bytes()
    with tarfile.open(first_path, "r:gz") as archive:
        names = archive.getnames()
        assert names == sorted(names)
        index_stream = archive.extractfile("index.json")
        assert index_stream is not None
        index = json.loads(index_stream.read())
    assert index["labId"] == "lab-package-sample"
    assert index["scenarios"] == ["lab-package-sample"]
    assert any(item["path"].endswith("authoring.yaml") for item in index["files"])
    assert not any(".git" in name or "kubelab.db" in name for name in names)


def test_package_normalizes_crlf_and_rejects_oversized_or_unsafe_archives(
    tmp_path: Path,
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-normalized-sample"
    _init(service, target, scenario_id="lab-normalized-sample")
    readme = target / "README.md"
    normalized = readme.read_bytes().replace(b"\r\n", b"\n")
    readme.write_bytes(normalized.replace(b"\n", b"\r\n"))
    first = service.package(target, output=tmp_path / "crlf.kubelab-lab.tar.gz")
    readme.write_bytes(normalized)
    second = service.package(target, output=tmp_path / "lf.kubelab-lab.tar.gz")
    assert first.passed and second.passed
    assert first.sha256 == second.sha256

    oversized = target / "oversized.txt"
    oversized.write_bytes(b"x" * (512 * 1024 + 1))
    rejected = service.package(target, output=tmp_path / "large.kubelab-lab.tar.gz")
    assert not rejected.passed
    assert not (tmp_path / "large.kubelab-lab.tar.gz").exists()

    unsafe = tmp_path / "unsafe.kubelab-lab.tar.gz"
    with tarfile.open(unsafe, "w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, BytesIO(b"x"))
    with pytest.raises(ValueError):
        AuthoringService._verify_package(unsafe)


def test_package_never_overwrites_and_integration_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-gate-sample"
    _init(service, target, scenario_id="lab-gate-sample")
    output = tmp_path / "owned.kubelab-lab.tar.gz"
    output.write_bytes(b"owned")

    packaged = service.package(target, output=output)
    monkeypatch.delenv("KUBELAB_RUN_LAB_INTEGRATION", raising=False)
    integration = service.test_integration(target)

    assert not packaged.passed
    assert output.read_bytes() == b"owned"
    assert not integration.passed
    assert integration.exit_code == 5
    assert integration.issues[0].code == "AUTHOR_INTEGRATION_DISABLED"


def test_package_rejects_variant_and_invalid_output_boundaries(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    parent = tmp_path / "lab-output-sample"
    _init(service, parent, scenario_id="lab-output-sample")
    variant = _init(
        service,
        parent,
        scenario_type="variant",
        scenario_id="variant-b",
    )

    variant_package = service.package(variant, output=tmp_path / "variant.kubelab-lab.tar.gz")
    wrong_suffix = service.package(parent, output=tmp_path / "package.zip")
    missing_parent = service.package(
        parent,
        output=tmp_path / "missing" / "package.kubelab-lab.tar.gz",
    )

    assert not variant_package.passed
    assert not wrong_suffix.passed
    assert not missing_parent.passed
    assert {wrong_suffix.issues[0].code, missing_parent.issues[0].code} == {
        "AUTHOR_PACKAGE_OUTPUT_INVALID"
    }


def test_integration_gate_rejects_non_wsl_before_loading_cluster_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-platform-gate"
    _init(service, target, scenario_id="lab-platform-gate")
    monkeypatch.setenv("KUBELAB_RUN_LAB_INTEGRATION", "1")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr("kubelab.authoring.platform.system", lambda: "Windows")

    report = service.test_integration(target)

    assert not report.passed
    assert report.exit_code == 5
    assert report.issues[0].code == "AUTHOR_INTEGRATION_PLATFORM_UNSUPPORTED"


def test_integration_gate_delegates_only_after_explicit_wsl_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-delegated-gate"
    _init(service, target, scenario_id="lab-delegated-gate")
    monkeypatch.setenv("KUBELAB_RUN_LAB_INTEGRATION", "1")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setattr("kubelab.authoring.platform.system", lambda: "Linux")
    calls: list[tuple[Path, Path | None]] = []

    def fake_runner(
        delegated: AuthoringService, delegated_target: Path, *, junit: Path | None
    ) -> object:
        assert delegated is service
        calls.append((delegated_target, junit))
        return service.test(delegated_target)

    monkeypatch.setattr("kubelab.authoring_integration.run_author_integration", fake_runner)

    report = service.test_integration(target, junit=None)

    assert report.passed
    assert calls == [(target, None)]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"valid": [{"Name": "minikube", "Config": {"Driver": "docker"}}]}, True),
        ({"Valid": [{"name": "minikube", "config": {"driver": "podman"}}]}, False),
        ({"valid": "not-a-list"}, False),
        ([], False),
    ],
)
def test_integration_profile_gate_accepts_only_exact_local_docker_profile(
    payload: object, expected: bool
) -> None:
    assert _docker_profile(payload, "minikube") is expected


def test_integration_runner_fails_closed_on_fake_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-prerequisite-sample"
    _init(service, target, scenario_id="lab-prerequisite-sample")
    issue = service._issue(
        "AUTHOR_INTEGRATION_ENVIRONMENT_UNAVAILABLE",
        ".",
        "The trusted local integration environment is unavailable.",
        exit_code=5,
    )
    monkeypatch.setattr(authoring_integration, "_integration_prerequisite", lambda delegated: issue)

    report = authoring_integration.run_author_integration(service, target, junit=None)

    assert not report.passed
    assert report.exit_code == 5
    assert report.issues == (issue,)


def test_integration_runner_batches_fake_scenarios_and_writes_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-integration-sample"
    _init(service, target, scenario_id="lab-integration-sample")
    calls: list[str] = []
    monkeypatch.setattr(authoring_integration, "_integration_prerequisite", lambda delegated: None)
    monkeypatch.setattr(
        authoring_integration,
        "_run_scenario",
        lambda delegated, registry, scenario: calls.append(scenario.scenario_id),
    )
    junit = tmp_path / "integration-result.xml"

    report = authoring_integration.run_author_integration(service, target, junit=junit)

    assert report.passed
    assert calls == ["lab-integration-sample"]
    assert junit.is_file()
    assert "lab-integration-sample" in junit.read_text(encoding="utf-8")


def test_integration_runner_sanitizes_scenario_failure_and_stops_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-integration-failure"
    _init(service, target, scenario_id="lab-integration-failure")
    monkeypatch.setattr(authoring_integration, "_integration_prerequisite", lambda delegated: None)

    def fail_safely(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Bearer private-integration-token")

    monkeypatch.setattr(authoring_integration, "_run_scenario", fail_safely)

    report = authoring_integration.run_author_integration(service, target, junit=None)

    assert not report.passed
    assert report.exit_code == 4
    assert report.results[0].passed is False
    assert report.issues[0].code == "AUTHOR_INTEGRATION_CONTRACT_FAILED"
    assert "private-integration-token" not in report.model_dump_json(by_alias=True)


def test_integration_junit_is_bounded_new_file_inside_workspace(tmp_path: Path) -> None:
    service = AuthoringService(tmp_path)
    target = tmp_path / "lab-junit-sample"
    _init(service, target, scenario_id="lab-junit-sample")
    report = service.test(target)
    destination = tmp_path / "author-junit.xml"

    issue = _write_junit(service, destination, report)

    assert issue is None
    content = destination.read_text(encoding="utf-8")
    assert "lab-junit-sample" in content
    assert "expected" not in content
    assert "actual" not in content
    second = _write_junit(service, destination, report)
    assert second is not None
    assert second.code == "AUTHOR_JUNIT_OUTPUT_INVALID"


def test_author_schema_rejects_unknown_fields_and_duplicate_yaml_keys() -> None:
    sample = (LABS_ROOT / "lab-001-deployment-scaling" / "authoring.yaml").read_text(
        encoding="utf-8"
    )
    parsed = load_all_unique(sample)[0]
    assert isinstance(parsed, dict)
    parsed["unknown"] = True
    with pytest.raises(ValidationError):
        LabAuthoringContract.model_validate(parsed)
    with pytest.raises(yaml.YAMLError):
        load_all_unique("apiVersion: one\napiVersion: two\n")


def test_author_cli_json_and_exit_codes_are_stable(tmp_path: Path) -> None:
    dry = RUNNER.invoke(
        app,
        [
            "lab",
            "init",
            str(tmp_path / "lab-cli-sample"),
            "--type",
            "baseline",
            "--id",
            "lab-cli-sample",
            "--title",
            "CLI样例",
            "--category",
            "workload",
            "--difficulty",
            "beginner",
            "--description",
            "CLI安全样例。",
            "--workspace",
            str(tmp_path),
            "--dry-run",
            "--json",
        ],
    )
    assert dry.exit_code == 0
    assert json.loads(dry.stdout)["dryRun"] is True
    assert not (tmp_path / "lab-cli-sample").exists()

    gated = RUNNER.invoke(
        app,
        [
            "lab",
            "test",
            str(LABS_ROOT / "lab-001-deployment-scaling"),
            "--workspace",
            str(LABS_ROOT.parent),
            "--integration",
            "--json",
        ],
        env={"KUBELAB_RUN_LAB_INTEGRATION": "0"},
    )
    assert gated.exit_code == 5
    payload = json.loads(gated.stdout)
    assert payload["issues"][0]["code"] == "AUTHOR_INTEGRATION_DISABLED"
    assert "exit_code" not in gated.stdout


def test_author_cli_requires_metadata_without_interactive_prompt(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "lab",
            "init",
            str(tmp_path / "lab-cli-missing"),
            "--type",
            "baseline",
            "--id",
            "lab-cli-missing",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "AUTHOR_INPUT_REQUIRED" in result.stderr
