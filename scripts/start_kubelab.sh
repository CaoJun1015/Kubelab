#!/usr/bin/env bash

set -euo pipefail

readonly web_url="http://127.0.0.1:8765"
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly repository_root="$(cd -- "$script_dir/.." && pwd -P)"
readonly state_root="${XDG_STATE_HOME:-$HOME/.local/state}/kubelab"
readonly pid_file="$state_root/server.pid"
readonly log_file="$state_root/server.log"

mode="start"
start_minikube=true

usage() {
    cat <<'EOF'
Usage: start_kubelab.sh [--web-only | --status | --stop]

  --web-only  Start only the KubeLab Web service; never start minikube.
  --status    Report whether the managed KubeLab Web service is running.
  --stop      Stop only the Web process started by this script.
  --help      Show this help text.

The script never changes KubeLab Context trust.
EOF
}

fail() {
    printf 'KubeLab startup error: %s\n' "$1" >&2
    exit 1
}

for argument in "$@"; do
    case "$argument" in
        --web-only)
            [[ "$mode" == "start" ]] || fail "--web-only cannot be combined with $mode."
            start_minikube=false
            ;;
        --status)
            [[ "$mode" == "start" && "$start_minikube" == "true" ]] \
                || fail "--status cannot be combined with another option."
            mode="status"
            ;;
        --stop)
            [[ "$mode" == "start" && "$start_minikube" == "true" ]] \
                || fail "--stop cannot be combined with another option."
            mode="stop"
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            fail "unsupported option: $argument"
            ;;
    esac
done

mkdir -p -- "$state_root"
chmod 700 -- "$state_root"
umask 077

read_managed_pid() {
    local candidate=""
    [[ -f "$pid_file" ]] || return 1
    IFS= read -r candidate < "$pid_file" || return 1
    [[ "$candidate" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$candidate"
}

is_kubelab_process() {
    local candidate="$1"
    local command_line=""
    [[ -r "/proc/$candidate/cmdline" ]] || return 1
    command_line="$(tr '\0' ' ' < "/proc/$candidate/cmdline")"
    [[ "$command_line" == *kubelab*serve* ]]
}

web_is_healthy() {
    local response=""
    response="$(curl --fail --silent --show-error --max-time 2 "$web_url/health" 2>/dev/null)" \
        || return 1
    grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<< "$response" \
        && grep -Eq '"version"[[:space:]]*:' <<< "$response"
}

managed_pid="$(read_managed_pid || true)"
if [[ -n "$managed_pid" ]] && ! kill -0 "$managed_pid" 2>/dev/null; then
    rm -f -- "$pid_file"
    managed_pid=""
fi

if [[ "$mode" == "status" ]]; then
    if [[ -n "$managed_pid" ]] && is_kubelab_process "$managed_pid" && web_is_healthy; then
        printf 'KubeLab Web is running (PID %s): %s/\n' "$managed_pid" "$web_url"
        exit 0
    fi
    printf 'KubeLab Web is not running under this script.\n'
    exit 1
fi

if [[ "$mode" == "stop" ]]; then
    [[ -n "$managed_pid" ]] || fail "no managed KubeLab Web process was found."
    is_kubelab_process "$managed_pid" \
        || fail "PID file does not identify a KubeLab serve process; refusing to signal it."
    kill "$managed_pid"
    for _ in {1..30}; do
        if ! kill -0 "$managed_pid" 2>/dev/null; then
            rm -f -- "$pid_file"
            printf 'KubeLab Web stopped. minikube was left unchanged.\n'
            exit 0
        fi
        sleep 0.2
    done
    fail "KubeLab Web did not stop; inspect $log_file."
fi

if [[ -n "$managed_pid" ]]; then
    is_kubelab_process "$managed_pid" \
        || fail "PID file points to another process; refusing to replace it."
    web_is_healthy \
        || fail "the managed process exists but its loopback health check failed."
    printf 'KubeLab Web is already running (PID %s): %s/\n' "$managed_pid" "$web_url"
    exit 0
fi

if web_is_healthy; then
    printf 'KubeLab Web is already available at %s/ (not managed by this script).\n' "$web_url"
    exit 0
fi

[[ "$(uname -s)" == "Linux" ]] || fail "run this script inside WSL2 Ubuntu."
grep -qi microsoft /proc/sys/kernel/osrelease \
    || fail "the supported runtime is WSL2 Ubuntu."
[[ -r /etc/os-release ]] || fail "cannot identify the Linux distribution."
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fail "the supported WSL distribution is Ubuntu."

for executable in curl docker minikube python3; do
    command -v "$executable" >/dev/null 2>&1 \
        || fail "required command '$executable' is not installed."
done

declare -a kubelab_command
if command -v uv >/dev/null 2>&1 && [[ -f "$repository_root/pyproject.toml" ]]; then
    cd -- "$repository_root"
    kubelab_command=(uv run kubelab)
elif command -v kubelab >/dev/null 2>&1; then
    kubelab_command=(kubelab)
else
    fail "kubelab is not installed; install its wheel with uv tool install first."
fi

if [[ "$start_minikube" == "true" ]]; then
    docker info >/dev/null 2>&1 \
        || fail "Docker daemon is unavailable; start it explicitly, then rerun this script."

    profile_report="$(minikube profile list --output=json 2>/dev/null)" \
        || fail "could not inspect the local minikube profiles."
    profile_driver="$(
        python3 -c '
import json
import sys

report = json.load(sys.stdin)
matches = [item for item in report.get("valid", []) if item.get("Name") == "minikube"]
if not matches:
    print("missing")
else:
    print(str(matches[0].get("Config", {}).get("Driver", "unknown")).lower())
' <<< "$profile_report"
    )" || fail "minikube returned an invalid profile report."

    case "$profile_driver" in
        missing)
            minikube start \
                --profile minikube \
                --driver=docker \
                --cpus=2 \
                --memory=4096
            ;;
        docker)
            status_report="$(minikube status --profile minikube --output=json 2>/dev/null || true)"
            if ! python3 -c '
import json
import sys

try:
    report = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(1)
expected = ("Running", "Running", "Running")
actual = (report.get("Host"), report.get("Kubelet"), report.get("APIServer"))
raise SystemExit(0 if actual == expected else 1)
' <<< "$status_report"; then
                minikube start --profile minikube
            fi
            ;;
        *)
            fail "the existing 'minikube' profile does not use the required Docker driver."
            ;;
    esac
fi

set +e
"${kubelab_command[@]}" doctor
doctor_exit=$?
set -e
case "$doctor_exit" in
    0)
        ;;
    3)
        printf '%s\n' \
            "KubeLab Doctor reports a blocked environment; Web will start so you can review fixed remediation guidance." >&2
        ;;
    *)
        fail "KubeLab Doctor failed with exit code $doctor_exit."
        ;;
esac

: > "$log_file"
nohup "${kubelab_command[@]}" serve </dev/null >> "$log_file" 2>&1 &
server_pid=$!
temporary_pid_file="$pid_file.$server_pid"
printf '%s\n' "$server_pid" > "$temporary_pid_file"
mv -f -- "$temporary_pid_file" "$pid_file"

startup_ok=false
for _ in {1..30}; do
    if web_is_healthy; then
        startup_ok=true
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [[ "$startup_ok" != "true" ]]; then
    if kill -0 "$server_pid" 2>/dev/null && is_kubelab_process "$server_pid"; then
        kill "$server_pid" 2>/dev/null || true
    fi
    rm -f -- "$pid_file"
    fail "Web health check did not become ready; inspect $log_file."
fi

printf 'KubeLab Web started (PID %s): %s/\n' "$server_pid" "$web_url"
printf 'Log: %s\n' "$log_file"
printf '%s\n' \
    "Context trust was not changed. If the environment page reports drift, inspect it before running 'kubelab context trust'."
