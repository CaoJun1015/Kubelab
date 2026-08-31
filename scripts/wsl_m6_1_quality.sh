#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "usage: wsl_m6_1_quality.sh" >&2
    exit 2
fi
if [[ ! -f /proc/version ]] || ! grep -qi microsoft /proc/version; then
    echo "M6.1 WSL quality gates are restricted to WSL2." >&2
    exit 2
fi
if ! grep -q '^ID=ubuntu$' /etc/os-release; then
    echo "M6.1 WSL quality gates require Ubuntu." >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
quality_root=$(mktemp -d "/tmp/kubelab-m6-1-quality.XXXXXX")
chmod 700 "$quality_root"

cd "$repo_root"
uv venv "$quality_root/venv" --python 3.11
export VIRTUAL_ENV="$quality_root/venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
export KUBELAB_RUN_INTEGRATION=0
export KUBELAB_RUN_LAB_INTEGRATION=0
unset KUBELAB_LAB_INTEGRATION_BATCH KUBELAB_LABS_DIR

uv sync --active --locked --dev
python -m pytest
ruff check .
ruff format --check .
mypy --strict src
node --check src/kubelab/static/app.js
git diff --check
uv build --out-dir "$quality_root/dist"
python scripts/verify_distribution.py \
    --wheel "$quality_root/dist/kubelab-0.3.0rc1-py3-none-any.whl" \
    --sdist "$quality_root/dist/kubelab-0.3.0rc1.tar.gz" \
    --version 0.3.0rc1

printf 'quality_root=%s\n' "$quality_root"
