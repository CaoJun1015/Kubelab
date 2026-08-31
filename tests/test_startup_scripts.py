"""Static safety contracts for the WSL/Windows KubeLab startup scripts."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_wsl_startup_is_idempotent_loopback_only_and_never_changes_trust() -> None:
    script = (ROOT / "scripts" / "start_kubelab.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8765" in script
    assert "0.0.0.0" not in script
    assert "web_is_healthy" in script
    assert "server.pid" in script
    assert "nohup" in script
    assert '"${kubelab_command[@]}" context trust' not in script
    assert "eval " not in script
    assert "bash -c" not in script


def test_wsl_startup_allows_blocked_doctor_but_rejects_unexpected_failures() -> None:
    script = (ROOT / "scripts" / "start_kubelab.sh").read_text(encoding="utf-8")

    assert 'case "$doctor_exit"' in script
    assert "3)" in script
    assert 'fail "KubeLab Doctor failed with exit code $doctor_exit."' in script
    assert "Web will start so you can review fixed remediation guidance" in script
    assert script.index("command -v uv") < script.index("command -v kubelab")


def test_wsl_startup_restricts_cluster_start_to_local_docker_profile() -> None:
    script = (ROOT / "scripts" / "start_kubelab.sh").read_text(encoding="utf-8")

    assert "docker info" in script
    assert "minikube profile list --output=json" in script
    assert "--driver=docker" in script
    assert "minikube start --profile minikube" in script
    assert "does not use the required Docker driver" in script
    assert "sudo" not in script


def test_windows_launcher_only_delegates_runtime_to_wsl() -> None:
    script = (ROOT / "scripts" / "start-kubelab.ps1").read_text(encoding="utf-8")

    assert "wsl.exe -d $Distribution -- wslpath" in script
    assert '$wslArguments = @("-d", $Distribution, "--", "bash", $wslScript)' in script
    assert "Start-Process $webUrl" in script
    assert "kubelab serve" not in script
    assert "minikube start" not in script


def test_distribution_verifier_requires_and_scans_startup_scripts() -> None:
    verifier = (ROOT / "scripts" / "verify_distribution.py").read_text(encoding="utf-8")

    assert '"scripts/start-kubelab.ps1"' in verifier
    assert '"scripts/start_kubelab.sh"' in verifier
    assert '".ps1"' in verifier
    assert '".sh"' in verifier
    assert "sdist is missing startup scripts" in verifier
