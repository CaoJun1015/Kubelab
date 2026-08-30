#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: wsl_release_smoke.sh ARTIFACT VERSION KUBECONFIG LABEL" >&2
    exit 2
fi

artifact=$1
expected_version=$2
kubeconfig=$3
label=$4
source_home=$HOME
server_pid=""
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ ! -f "$artifact" || ! -f "$kubeconfig" ]]; then
    echo "artifact and kubeconfig must be existing files" >&2
    exit 2
fi
if [[ ! "$label" =~ ^[a-z0-9-]+$ ]]; then
    echo "label must contain only lowercase letters, digits, and hyphens" >&2
    exit 2
fi

release_root=$(mktemp -d "/tmp/kubelab-release-${label}.XXXXXX")
chmod 700 "$release_root"
stop_server() {
    if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid"
        wait "$server_pid" || true
    fi
}
finish() {
    status=$?
    stop_server
    printf 'release_root=%s\n' "$release_root"
    exit "$status"
}
trap finish EXIT
mkdir -p \
    "$release_root/home" \
    "$release_root/config" \
    "$release_root/state" \
    "$release_root/tools" \
    "$release_root/bin" \
    "$release_root/cache" \
    "$release_root/work"

export HOME="$release_root/home"
export XDG_CONFIG_HOME="$release_root/config"
export XDG_STATE_HOME="$release_root/state"
export UV_TOOL_DIR="$release_root/tools"
export UV_TOOL_BIN_DIR="$release_root/bin"
export UV_CACHE_DIR="$release_root/cache"
export KUBELAB_KUBECONFIG="$kubeconfig"
export KUBECONFIG="$kubeconfig"
export MINIKUBE_HOME="${MINIKUBE_HOME:-$source_home}"
unset KUBELAB_LABS_DIR KUBELAB_RUN_INTEGRATION KUBELAB_RUN_LAB_INTEGRATION
cd "$release_root/work"

uv tool install --python 3.11 "$artifact"
kubelab_bin="$UV_TOOL_BIN_DIR/kubelab"

version_output=$($kubelab_bin --version)
if [[ "$version_output" != "KubeLab $expected_version" ]]; then
    echo "unexpected version: $version_output" >&2
    exit 1
fi

set +e
$kubelab_bin doctor --json > "$release_root/doctor.json"
doctor_exit=$?
set -e
if [[ "$doctor_exit" -ne 0 && "$doctor_exit" -ne 3 ]]; then
    echo "Doctor returned an unexpected exit code: $doctor_exit" >&2
    exit 1
fi
$kubelab_bin list --json > "$release_root/labs.json"
read -r doctor_status lab_count variant_count scenario_count < <(
    python3 "$script_dir/validate_release_smoke.py" \
        --doctor "$release_root/doctor.json" \
        --catalog "$release_root/labs.json" \
        --doctor-exit "$doctor_exit"
)

if [[ "$doctor_exit" -eq 0 ]]; then
    $kubelab_bin context inspect --json > "$release_root/context-before.json"
    $kubelab_bin context trust > "$release_root/context-trust.txt"
    $kubelab_bin context inspect --json > "$release_root/context-after.json"
    context_status=$(python3 - "$release_root/context-after.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report.get("trust_state") != "trusted":
    raise SystemExit("Isolated Context trust was not persisted")
print("trusted")
PY
    )
else
    context_status="skipped-not-ready"
fi

$kubelab_bin serve > "$release_root/server.log" 2>&1 &
server_pid=$!

for _ in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8765/health \
        > "$release_root/health.json" 2>/dev/null; then
        break
    fi
    sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:8765/ > "$release_root/dashboard.html"
curl --fail --silent --show-error http://127.0.0.1:8765/labs > "$release_root/labs.html"
curl --fail --silent --show-error http://127.0.0.1:8765/static/app.js \
    > "$release_root/app.js"
if ! ss -ltnp | grep -q '127.0.0.1:8765'; then
    echo "Web server is not listening on the required loopback address" >&2
    exit 1
fi

stop_server
if kill -0 "$server_pid" 2>/dev/null; then
    echo "Web server process did not stop" >&2
    exit 1
fi

printf 'artifact=%s\n' "$artifact"
printf 'version=%s\n' "$version_output"
printf 'labs=%s\n' "$lab_count"
printf 'variants=%s\n' "$variant_count"
printf 'scenarios=%s\n' "$scenario_count"
printf 'doctor=%s (exit=%s)\n' "$doctor_status" "$doctor_exit"
printf 'context=%s\n' "$context_status"
printf 'web=127.0.0.1:8765 stopped-cleanly\n'
