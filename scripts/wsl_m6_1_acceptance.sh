#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "usage: wsl_m6_1_acceptance.sh" >&2
    exit 2
fi
if [[ ! -f /proc/version ]] || ! grep -qi microsoft /proc/version; then
    echo "M6.1 real acceptance is restricted to WSL2." >&2
    exit 2
fi
if ! grep -q '^ID=ubuntu$' /etc/os-release; then
    echo "M6.1 real acceptance requires Ubuntu." >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
validator="$script_dir/validate_integration_acceptance.py"
results_root=$(mktemp -d "/tmp/kubelab-m6-1-acceptance.XXXXXX")
chmod 700 "$results_root"
initial_status=""

restore_profile() {
    status=$?
    trap - EXIT
    if [[ "$initial_status" == "Stopped" ]]; then
        if ! minikube stop --profile minikube > "$results_root/minikube-stop.log" 2>&1; then
            echo "Failed to restore the initially stopped minikube profile." >&2
            status=1
        fi
    fi
    printf 'results_root=%s\n' "$results_root"
    exit "$status"
}
trap restore_profile EXIT

for executable in docker minikube kubectl uv python3 git; do
    if ! command -v "$executable" >/dev/null 2>&1; then
        echo "Missing required executable: $executable" >&2
        exit 2
    fi
done

cd "$repo_root"
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Acceptance requires a clean tracked working tree." >&2
    exit 1
fi
candidate_commit=$(git rev-parse HEAD)
uv venv "$results_root/venv" --python 3.11
export VIRTUAL_ENV="$results_root/venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
uv sync --active --locked --dev
minikube profile list --output json > "$results_root/profile-before.json"
read -r initial_status driver < <(
    python3 "$validator" profile --path "$results_root/profile-before.json"
)
if [[ "$driver" != "docker" ]]; then
    echo "The minikube profile is not using Docker." >&2
    exit 1
fi
if [[ "$initial_status" == "Stopped" ]]; then
    minikube start --profile minikube > "$results_root/minikube-start.log" 2>&1
fi

if [[ "$(kubectl config current-context)" != "minikube" ]]; then
    echo "Current Kubernetes Context must remain minikube." >&2
    exit 1
fi
kubelab context inspect --json > "$results_root/context.json"
python3 "$validator" context --path "$results_root/context.json" > "$results_root/context-status.txt"
if [[ "$(kubelab --version)" != "KubeLab 0.3.0rc1" ]]; then
    echo "The acceptance environment is not running KubeLab 0.3.0rc1." >&2
    exit 1
fi
kubelab doctor --json > "$results_root/doctor.json"
python3 "$validator" doctor --path "$results_root/doctor.json" \
    > "$results_root/doctor-status.txt"

minikube image ls --profile minikube > "$results_root/images.txt"
for image in nginx:1.26-alpine nginx:1.27-alpine busybox:1.36.1 curlimages/curl:8.12.1; do
    if ! grep -Fq "$image" "$results_root/images.txt"; then
        echo "Required fixed image is not cached: $image" >&2
        exit 1
    fi
done
kubectl --namespace ingress-nginx rollout status \
    deployment/ingress-nginx-controller --timeout=120s > "$results_root/ingress.txt"
kubectl get storageclass standard -o json > "$results_root/storageclass.json"
kubectl --namespace kube-system get pods -o json > "$results_root/kube-system-pods.json"
python3 - "$results_root/storageclass.json" "$results_root/kube-system-pods.json" <<'PY'
import json
import sys
from pathlib import Path

storage = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
annotations = storage.get("metadata", {}).get("annotations", {})
if annotations.get("storageclass.kubernetes.io/is-default-class") != "true" and annotations.get(
    "storageclass.beta.kubernetes.io/is-default-class"
) != "true":
    raise SystemExit("The standard StorageClass is not the default")
pods = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")).get("items", [])
if not any(
    pod.get("metadata", {}).get("name", "").startswith("storage-provisioner")
    and pod.get("status", {}).get("phase") == "Running"
    for pod in pods
):
    raise SystemExit("The minikube storage provisioner is not Running")
PY

if ! python3 "$validator" audit > "$results_root/preflight-audit.json"; then
    echo "Pre-existing KubeLab residue must be reviewed before acceptance." >&2
    cat "$results_root/preflight-audit.json" >&2
    exit 1
fi

export KUBELAB_RUN_LAB_INTEGRATION=1
unset KUBELAB_LABS_DIR KUBELAB_RUN_INTEGRATION
batches=(baseline-001-012 baseline-013-021 variants-013-015 variants-016-018)
: > "$results_root/batches.tsv"
for batch in "${batches[@]}"; do
    if [[ "$(git rev-parse HEAD)" != "$candidate_commit" ]] \
        || ! git diff --quiet || ! git diff --cached --quiet; then
        echo "Candidate content changed; all four batches must restart from a clean commit." >&2
        exit 1
    fi
    export KUBELAB_LAB_INTEGRATION_BATCH="$batch"
    started_at=$(date +%s)
    junit="$results_root/${batch}.xml"
    python -m pytest \
        tests/test_first_labs_integration.py::test_real_fault_repair_reset_cleanup_contract \
        --no-cov -q --junitxml="$junit"
    python3 "$validator" junit --path "$junit" --batch "$batch" \
        > "$results_root/${batch}-junit.json"

    audit_ok=0
    for _ in {1..12}; do
        if python3 "$validator" audit > "$results_root/${batch}-audit.json"; then
            audit_ok=1
            break
        fi
        sleep 5
    done
    if [[ "$audit_ok" -ne 1 ]]; then
        echo "KubeLab residue remains after batch $batch; it was not removed automatically." >&2
        cat "$results_root/${batch}-audit.json" >&2
        exit 1
    fi
    duration=$(( $(date +%s) - started_at ))
    printf '%s\t%s\n' "$batch" "$duration" >> "$results_root/batches.tsv"
done

unset KUBELAB_LAB_INTEGRATION_BATCH KUBELAB_RUN_LAB_INTEGRATION
printf 'candidate_commit=%s\n' "$candidate_commit"
printf 'batches=12,9,6,6\n'
printf 'residue=zero\n'
