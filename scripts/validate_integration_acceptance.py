#!/usr/bin/env python3
"""Validate fixed-batch integration evidence and audit local cluster residue."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

BATCH_COUNTS = {
    "baseline-001-012": 12,
    "baseline-013-021": 9,
    "variants-013-015": 6,
    "variants-016-018": 6,
}


def validate_junit(path: Path, expected: int) -> dict[str, int]:
    """Require an exact, entirely successful scenario batch."""
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    failures = root.findall(".//failure")
    errors = root.findall(".//error")
    skipped = root.findall(".//skipped")
    summary = {
        "tests": len(cases),
        "failures": len(failures),
        "errors": len(errors),
        "skipped": len(skipped),
    }
    if summary != {"tests": expected, "failures": 0, "errors": 0, "skipped": 0}:
        raise ValueError("The JUnit batch did not contain the exact all-pass scenario count.")
    return summary


def validate_profile(report: dict[str, Any]) -> tuple[str, str]:
    """Resolve the one existing local minikube profile and Docker driver."""
    profiles = report.get("valid")
    if not isinstance(profiles, list):
        raise ValueError("The minikube profile report is invalid.")
    matches = [
        item for item in profiles if isinstance(item, dict) and item.get("Name") == "minikube"
    ]
    if len(matches) != 1:
        raise ValueError("Exactly one valid minikube profile is required.")
    profile = matches[0]
    config = profile.get("Config")
    if not isinstance(config, dict) or config.get("Driver") != "docker":
        raise ValueError("The minikube profile must use the local Docker driver.")
    raw_status = profile.get("Status")
    statuses = {"OK": "Running", "Running": "Running", "Stopped": "Stopped"}
    if raw_status not in statuses:
        raise ValueError("The minikube profile must already be Running or Stopped.")
    return statuses[raw_status], "docker"


def validate_context(report: dict[str, Any]) -> None:
    """Fail closed when the trusted local profile identity has drifted."""
    if (
        report.get("context_name") != "minikube"
        or report.get("minikube_profile") != "minikube"
        or report.get("trust_state") != "trusted"
        or report.get("trusted") is not True
    ):
        raise ValueError("The current minikube Context is not already trusted.")


def residue_from_reports(
    namespaces: dict[str, Any],
    namespaced: dict[str, Any],
    persistent_volumes: dict[str, Any],
    temporary_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    """Return only identifiers for KubeLab residue; never mutate the cluster."""
    residue: set[str] = set()
    for item in _items(namespaces):
        metadata = _metadata(item)
        if _managed(metadata):
            residue.add(f"Namespace/{metadata.get('name', 'unknown')}")

    for item in _items(namespaced):
        metadata = _metadata(item)
        kind = str(item.get("kind", "Unknown"))
        name = str(metadata.get("name", "unknown"))
        namespace = str(metadata.get("namespace", "unknown"))
        is_workspace_rbac = kind in {"ServiceAccount", "Role", "RoleBinding"} and name == (
            "kubelab-workspace"
        )
        is_probe = kind == "Pod" and name.startswith("kubelab-probe-")
        is_lab_pvc = kind == "PersistentVolumeClaim" and namespace.startswith("kubelab-")
        if _managed(metadata) or is_workspace_rbac or is_probe or is_lab_pvc:
            residue.add(f"{kind}/{namespace}/{name}")

    for item in _items(persistent_volumes):
        metadata = _metadata(item)
        spec = item.get("spec")
        claim = spec.get("claimRef") if isinstance(spec, dict) else None
        claim_namespace = claim.get("namespace") if isinstance(claim, dict) else None
        if _managed(metadata) or (
            isinstance(claim_namespace, str) and claim_namespace.startswith("kubelab-")
        ):
            residue.add(f"PersistentVolume/{metadata.get('name', 'unknown')}")

    residue.update(f"TemporaryPath/{path.name}" for path in temporary_paths)
    return tuple(sorted(residue))


def _items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = report.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("A kubectl audit report is malformed.")
    return items


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _managed(metadata: dict[str, Any]) -> bool:
    labels = metadata.get("labels")
    return isinstance(labels, dict) and labels.get("kubelab.io/managed-by") == "kubelab"


def _kubectl_json(*arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["kubectl", *arguments, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("kubectl did not return a JSON object.")
    return value


def audit_cluster() -> tuple[str, ...]:
    return residue_from_reports(
        _kubectl_json("get", "namespaces"),
        _kubectl_json(
            "get",
            "serviceaccounts,roles,rolebindings,persistentvolumeclaims,pods",
            "--all-namespaces",
        ),
        _kubectl_json("get", "persistentvolumes"),
        tuple(Path("/tmp").glob("kubelab-workspace-*")),
    )


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("The report must be a JSON object.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    junit = subparsers.add_parser("junit")
    junit.add_argument("--path", required=True, type=Path)
    junit.add_argument("--batch", required=True, choices=tuple(BATCH_COUNTS))
    profile = subparsers.add_parser("profile")
    profile.add_argument("--path", required=True, type=Path)
    context = subparsers.add_parser("context")
    context.add_argument("--path", required=True, type=Path)
    subparsers.add_parser("audit")
    args = parser.parse_args()

    try:
        if args.command == "junit":
            print(json.dumps(validate_junit(args.path, BATCH_COUNTS[args.batch]), sort_keys=True))
        elif args.command == "profile":
            print(*validate_profile(_load_object(args.path)))
        elif args.command == "context":
            validate_context(_load_object(args.path))
            print("trusted")
        else:
            residue = audit_cluster()
            print(json.dumps({"residue_count": len(residue), "residue": residue}, sort_keys=True))
            if residue:
                return 1
    except (ET.ParseError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"acceptance validation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
