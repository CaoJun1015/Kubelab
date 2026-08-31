#!/usr/bin/env python3
"""Verify a KubeLab database upgrade on a temporary SQLite backup copy only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from kubelab.database import Database

ALLOWED_REVISIONS = {
    "0001_initial_persistence",
    "0002_guided_learning",
    "0003_lab_variants",
}
TARGET_REVISION = "0003_lab_variants"


def _revision(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("The source database has no Alembic revision.")
    return row[0]


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = (
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
        )
    )
    return {
        name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in names
    }


def _signature(path: Path) -> tuple[int, str] | None:
    if not path.exists():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path.stat().st_size, digest


def _source_signatures(path: Path) -> dict[str, tuple[int, str] | None]:
    """Hash durable SQLite content; the shared-memory lock file is intentionally transient."""
    return {
        "database": _signature(path),
        "wal": _signature(path.with_name(f"{path.name}-wal")),
    }


def verify_copy_upgrade(source: Path) -> dict[str, Any]:
    """Copy with SQLite's backup API, upgrade the copy, and return safe counts only."""
    source = source.resolve(strict=True)
    before_signatures = _source_signatures(source)
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as connection:
        source_revision = _revision(connection)
        if source_revision not in ALLOWED_REVISIONS:
            raise RuntimeError("The source database revision is not supported by M6.1.")
        source_counts = _table_counts(connection)

        with tempfile.TemporaryDirectory(prefix="kubelab-m6-1-upgrade-") as temporary:
            candidate = Path(temporary) / "kubelab.db"
            with closing(sqlite3.connect(candidate)) as destination:
                connection.backup(destination)

            database = Database(
                candidate,
                lock_path=Path(temporary) / "operations.lock",
                lock_timeout_seconds=0,
            )
            try:
                database.initialize()
            finally:
                database.dispose()

            with closing(sqlite3.connect(candidate)) as upgraded:
                target_revision = _revision(upgraded)
                if target_revision != TARGET_REVISION:
                    raise RuntimeError("The copied database did not reach the M6 revision.")
                target_counts = _table_counts(upgraded)
                for table, count in source_counts.items():
                    if target_counts.get(table) != count:
                        raise RuntimeError("A persisted table count changed during copied upgrade.")
                indexes = {row[1] for row in upgraded.execute("PRAGMA index_list('lab_session')")}
                if "ix_lab_session_lab_variant_created" not in indexes:
                    raise RuntimeError("The copied database is missing the variant query index.")
                if source_revision != TARGET_REVISION:
                    invalid_variants = upgraded.execute(
                        "SELECT COUNT(*) FROM lab_session "
                        "WHERE variant_id IS NULL OR variant_id != 'baseline'"
                    ).fetchone()[0]
                    if invalid_variants:
                        raise RuntimeError("A legacy Session was not backfilled to baseline.")

            backup_created = candidate.with_name("kubelab.db.bak").is_file()
            if backup_created != (source_revision != TARGET_REVISION):
                raise RuntimeError("The copied upgrade backup behavior was unexpected.")

    if _source_signatures(source) != before_signatures:
        raise RuntimeError("The source database changed during copied upgrade verification.")
    return {
        "source_revision": source_revision,
        "target_revision": TARGET_REVISION,
        "preserved_table_count": len(source_counts),
        "session_count": source_counts.get("lab_session", 0),
        "backup_created": source_revision != TARGET_REVISION,
        "source_unchanged": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a KubeLab database upgrade without modifying the source database."
    )
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_copy_upgrade(args.source), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
