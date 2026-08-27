"""Deterministic JSON Schema export for KubeLab lab definitions."""

from __future__ import annotations

import json
from pathlib import Path

from kubelab.lab_schema import LabDefinition


def render_lab_json_schema() -> str:
    """Return the canonical, reproducible v1alpha1 JSON Schema."""
    schema = LabDefinition.model_json_schema(by_alias=True, mode="validation")
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def default_schema_path() -> Path:
    """Return the project schema path without writing it."""
    return Path(__file__).resolve().parents[2] / "schemas" / "lab-v1alpha1.schema.json"


def main() -> None:
    """Write the canonical schema for maintainers."""
    path = default_schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_lab_json_schema(), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
