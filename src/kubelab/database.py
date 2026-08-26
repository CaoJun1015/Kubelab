"""SQLite engine setup, Alembic migration, and safe backup orchestration."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from kubelab.config import get_data_dir
from kubelab.operation_lock import OperationLock
from kubelab.repositories import SqlAlchemyUnitOfWork


class DatabaseError(RuntimeError):
    code = "DATABASE_ERROR"


class Database:
    """Own the local SQLite engine and schema migration lifecycle."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        lock_path: Path | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        data_dir = get_data_dir() if path is None else path.parent
        self.path = path or data_dir / "kubelab.db"
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.lock_path = lock_path or data_dir / "operations.lock"
        self._operation_lock = OperationLock(self.lock_path, timeout_seconds=lock_timeout_seconds)
        url = URL.create("sqlite+pysqlite", database=str(self.path))
        self.engine = create_engine(url, future=True)
        event.listen(self.engine, "connect", _configure_sqlite)
        event.listen(self.engine, "begin", _begin_sqlite)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    def initialize(self) -> None:
        """Apply pending migrations under the global operation lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed_before = self.path.exists() and self.path.stat().st_size > 0
        with self._operation_lock:
            config = self._alembic_config()
            try:
                current, head = self._revisions(config)
                if existed_before and current != head:
                    self._checkpoint_and_backup()
                with self.engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
            except Exception as exc:
                raise DatabaseError("Failed to initialize the KubeLab database.") from exc

    def unit_of_work(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.session_factory)

    def dispose(self) -> None:
        self.engine.dispose()

    def _alembic_config(self) -> Config:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parent / "migrations")
        )
        config.set_main_option(
            "sqlalchemy.url", self.engine.url.render_as_string(hide_password=False)
        )
        return config

    def _revisions(self, config: Config) -> tuple[str | None, str | None]:
        with self.engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        head = ScriptDirectory.from_config(config).get_current_head()
        return current, head

    def _checkpoint_and_backup(self) -> None:
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")
        self.engine.dispose()
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".bak.tmp",
            )
            os.close(descriptor)
            temporary = Path(name)
            shutil.copy2(self.path, temporary)
            os.replace(temporary, self.backup_path)
        except OSError as exc:
            raise DatabaseError("Failed to create the pre-migration database backup.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    dbapi_connection.isolation_level = None  # type: ignore[attr-defined]
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _begin_sqlite(connection: Connection) -> None:
    connection.exec_driver_sql("BEGIN")


def sqlite_pragmas(connection: Connection) -> dict[str, int | str]:
    """Read effective SQLite safety settings for diagnostics and tests."""
    return {
        "foreign_keys": int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()),
        "busy_timeout": int(connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()),
        "journal_mode": str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()),
    }


__all__ = ["Database", "DatabaseError", "sqlite_pragmas"]
