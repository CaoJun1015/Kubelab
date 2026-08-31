#!/usr/bin/env python3
"""Validate structured Doctor and catalog output for an installed release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_reports(
    doctor: dict[str, Any], catalog: dict[str, Any], doctor_exit: int
) -> tuple[str, int, int, int]:
    """Return the stable smoke summary or reject an inconsistent report."""
    status = doctor.get("status")
    if doctor_exit == 3 and status != "unhealthy":
        raise ValueError("Doctor exit 3 must contain an unhealthy report.")
    if doctor_exit == 0 and status not in {"healthy", "degraded"}:
        raise ValueError("Doctor exit 0 must contain a usable report.")
    if doctor_exit not in {0, 3}:
        raise ValueError("Doctor must exit with zero or three.")

    labs = catalog.get("labs")
    if not isinstance(labs, list) or catalog.get("errors") != []:
        raise ValueError("The installed registry report is malformed or contains errors.")
    variant_counts = [lab.get("variant_total") for lab in labs if isinstance(lab, dict)]
    if len(variant_counts) != len(labs) or any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in variant_counts
    ):
        raise ValueError("The installed registry contains an invalid variant count.")

    lab_count = len(labs)
    variant_count = sum(variant_counts)
    if lab_count != 21 or variant_count != 12:
        raise ValueError("The installed registry must contain 21 labs and 12 variants.")
    return str(status), lab_count, variant_count, lab_count + variant_count


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("A release smoke report must be a JSON object.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doctor", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--doctor-exit", required=True, type=int)
    args = parser.parse_args()
    summary = validate_reports(
        _load_object(args.doctor),
        _load_object(args.catalog),
        args.doctor_exit,
    )
    print(*summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
