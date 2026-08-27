"""SQLite, Alembic, Repository, and UnitOfWork integration tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import delete, inspect, select, text

from kubelab.database import Database, DatabaseError, sqlite_pragmas
from kubelab.db_models import (
    CheckResultRecord,
    HintUsageRecord,
    LabSessionRecord,
    RetrospectiveRecord,
    SessionEventRecord,
    VerificationRunRecord,
)
from kubelab.repositories import ActiveSessionConflict, SessionNotFoundError
from kubelab.session_state import (
    CheckResultInput,
    InvalidSessionTransition,
    NewLabSession,
    RetrospectiveInput,
    SessionStatus,
    ValidationStatus,
    VerificationPurpose,
    VerificationRunInput,
)


def make_database(tmp_path: Path) -> Database:
    database = Database(
        tmp_path / "state" / "kubelab.db",
        lock_path=tmp_path / "state" / "operations.lock",
        lock_timeout_seconds=0,
    )
    database.initialize()
    return database


def new_session(session_id: str = "00000000-0000-4000-8000-000000000001") -> NewLabSession:
    return NewLabSession(
        id=session_id,
        lab_id="database-lab",
        namespace="kubelab-database-lab",
        context_name="minikube",
        context_fingerprint="a" * 64,
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
    )


def test_initialize_creates_all_tables_and_required_pragmas(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            "alembic_version",
            "check_result",
            "hint_usage",
            "lab_session",
            "retrospective",
            "session_event",
            "verification_run",
        }
        with database.engine.connect() as connection:
            assert sqlite_pragmas(connection) == {
                "foreign_keys": 1,
                "busy_timeout": 5000,
                "journal_mode": "wal",
            }
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0001_initial_persistence"
    finally:
        database.dispose()


def test_initialize_is_idempotent_and_current_database_is_not_backed_up(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    database.initialize()

    assert database.backup_path.exists() is False
    database.dispose()


def test_pending_migration_creates_checkpointed_backup(tmp_path: Path) -> None:
    path = tmp_path / "state" / "kubelab.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('preserved')")
    database = Database(path, lock_path=path.parent / "operations.lock", lock_timeout_seconds=0)

    database.initialize()

    assert database.backup_path.is_file()
    with sqlite3.connect(database.backup_path) as connection:
        assert connection.execute("SELECT value FROM legacy_marker").fetchone() == ("preserved",)
    database.dispose()


def test_backup_failure_is_reported_without_deleting_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kubelab.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT)")
    database = Database(path, lock_path=tmp_path / "operations.lock", lock_timeout_seconds=0)

    def fail_copy(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated copy failure")

    monkeypatch.setattr("kubelab.database.shutil.copy2", fail_copy)

    with pytest.raises(DatabaseError, match="initialize"):
        database.initialize()

    assert path.exists()
    assert database.backup_path.exists() is False
    database.dispose()


def test_migration_failure_is_converted_to_database_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(
        tmp_path / "kubelab.db",
        lock_path=tmp_path / "operations.lock",
        lock_timeout_seconds=0,
    )

    def fail_upgrade(config: Any, revision: str) -> None:
        del config, revision
        raise RuntimeError("migration failed")

    monkeypatch.setattr(command, "upgrade", fail_upgrade)

    with pytest.raises(DatabaseError, match="initialize"):
        database.initialize()
    database.dispose()


def test_session_create_transition_events_and_completion(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with database.unit_of_work() as uow:
            created = uow.sessions.create(new_session())
            assert created.status is SessionStatus.PROVISIONING
            ready = uow.sessions.transition(
                created.id, SessionStatus.READY, event_type="environment_ready"
            )
            in_progress = uow.sessions.transition(
                created.id, SessionStatus.IN_PROGRESS, event_type="session_observed"
            )
            passed = uow.sessions.transition(
                created.id, SessionStatus.PASSED, event_type="verification_passed"
            )
            cleaning = uow.sessions.transition(
                created.id, SessionStatus.CLEANING, event_type="cleanup_started"
            )
            completed = uow.sessions.transition(
                created.id, SessionStatus.COMPLETED, event_type="cleanup_completed"
            )
            events = uow.sessions.list_events(created.id)
            uow.commit()

        assert ready.started_at is not None
        assert in_progress.status is SessionStatus.IN_PROGRESS
        assert passed.status is SessionStatus.PASSED
        assert cleaning.status is SessionStatus.CLEANING
        assert completed.completed_at is not None
        assert [event.event_type for event in events] == [
            "session_created",
            "environment_ready",
            "session_observed",
            "verification_passed",
            "cleanup_started",
            "cleanup_completed",
        ]
        assert events[0].from_status is None
        assert events[-1].to_status is SessionStatus.COMPLETED

        with database.unit_of_work() as uow:
            assert uow.sessions.get_active() is None
            assert uow.sessions.require(created.id).status is SessionStatus.COMPLETED
    finally:
        database.dispose()


def test_invalid_transition_and_missing_session_do_not_write_events(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with database.unit_of_work() as uow:
            created = uow.sessions.create(new_session())
            with pytest.raises(InvalidSessionTransition):
                uow.sessions.transition(
                    created.id, SessionStatus.PASSED, event_type="invalid_transition"
                )
            with pytest.raises(SessionNotFoundError):
                uow.sessions.require("missing")
            assert len(uow.sessions.list_events(created.id)) == 1
            uow.commit()
    finally:
        database.dispose()


def test_database_constraint_blocks_second_active_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with database.unit_of_work() as uow:
            first = uow.sessions.create(new_session())
            uow.commit()

        with database.unit_of_work() as uow:
            with pytest.raises(ActiveSessionConflict) as caught:
                uow.sessions.create(new_session("00000000-0000-4000-8000-000000000002"))
            assert caught.value.active is not None
            assert caught.value.active.id == first.id

        with database.engine.begin() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM lab_session")).scalar_one()
        assert count == 1
    finally:
        database.dispose()


def test_concurrent_session_creation_allows_exactly_one_winner(tmp_path: Path) -> None:
    database = make_database(tmp_path)

    def create_one(session_id: str) -> str:
        try:
            with database.unit_of_work() as uow:
                uow.sessions.create(new_session(session_id))
                uow.commit()
            return "created"
        except ActiveSessionConflict:
            return "conflict"

    try:
        identifiers = (
            "00000000-0000-4000-8000-000000000021",
            "00000000-0000-4000-8000-000000000022",
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(create_one, identifiers))

        assert sorted(outcomes) == ["conflict", "created"]
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM lab_session")).scalar_one() == 1
    finally:
        database.dispose()


def test_completed_session_allows_new_active_session(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with database.unit_of_work() as uow:
            first = uow.sessions.create(new_session())
            uow.sessions.transition(first.id, SessionStatus.CLEANING, event_type="cleanup")
            uow.sessions.transition(first.id, SessionStatus.COMPLETED, event_type="completed")
            uow.commit()
        with database.unit_of_work() as uow:
            second = uow.sessions.create(new_session("00000000-0000-4000-8000-000000000002"))
            uow.commit()
        assert second.status is SessionStatus.PROVISIONING
    finally:
        database.dispose()


def test_error_context_is_redacted_and_reset_count_updates(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with database.unit_of_work() as uow:
            created = uow.sessions.create(new_session())
            errored = uow.sessions.transition(
                created.id,
                SessionStatus.ERROR,
                event_type="provisioning_failed",
                context={"authorization": "Bearer secret"},
                error_code="APPLY_FAILED",
                error_context={"token": "secret", "reason": "timeout"},
            )
            uow.sessions.transition(created.id, SessionStatus.RESETTING, event_type="reset_started")
            assert uow.sessions.increment_reset_count(created.id) == 1
            ready = uow.sessions.transition(
                created.id, SessionStatus.READY, event_type="reset_completed"
            )
            events = uow.sessions.list_events(created.id)
            uow.commit()

        assert errored.last_error_code == "APPLY_FAILED"
        assert errored.last_error_context == {"token": "[REDACTED]", "reason": "timeout"}
        assert events[1].context == {"authorization": "[REDACTED]"}
        assert ready.reset_count == 1
        assert ready.last_error_code is None
    finally:
        database.dispose()


def test_uow_rolls_back_exception_and_uncommitted_normal_exit(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="abort"):
            with database.unit_of_work() as uow:
                uow.sessions.create(new_session())
                raise RuntimeError("abort")
        with database.unit_of_work() as uow:
            assert uow.sessions.get_active() is None
            uow.sessions.create(new_session())
        with database.unit_of_work() as uow:
            assert uow.sessions.get_active() is None
    finally:
        database.dispose()


def test_verification_hint_and_retrospective_persistence_is_redacted(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        session_id = new_session().id
        run = VerificationRunInput(
            id="00000000-0000-4000-8000-000000000010",
            session_id=session_id,
            purpose=VerificationPurpose.MANUAL,
            status=ValidationStatus.FAILED,
            reset_sequence=0,
            duration_ms=15,
            results=(
                CheckResultInput(
                    check_id="config-fixed",
                    check_type="config_value",
                    status=ValidationStatus.FAILED,
                    expected={"password": "expected-secret"},
                    actual={"token": "actual-secret"},
                    message="Bearer abc.def failed",
                    retryable=True,
                    duration_ms=12,
                ),
            ),
        )
        with database.unit_of_work() as uow:
            uow.sessions.create(new_session())
            uow.verifications.add(run)
            assert uow.hints.record_once(session_id, 1) is True
            assert uow.hints.record_once(session_id, 1) is False
            retrospective = uow.retrospectives.save(
                session_id,
                RetrospectiveInput(
                    symptom="Bearer abc.def was shown",
                    root_cause="token=secret",
                    resolution="fixed",
                ),
            )
            uow.commit()

        assert retrospective.symptom == "Bearer [REDACTED] was shown"
        with database.session_factory() as session:
            result = session.scalar(select(CheckResultRecord))
            assert result is not None
            assert result.expected == {"password": "[REDACTED]"}
            assert result.actual == {"token": "[REDACTED]"}
            assert result.message == "Bearer [REDACTED] failed"
            assert session.scalar(select(VerificationRunRecord)) is not None
            assert session.scalar(select(HintUsageRecord)) is not None
            assert session.scalar(select(RetrospectiveRecord)) is not None
        with database.unit_of_work() as uow:
            loaded = uow.retrospectives.get(session_id)
            assert loaded is not None
            assert loaded.resolution == "fixed"
            updated = uow.retrospectives.save(
                session_id, RetrospectiveInput(resolution="fixed twice")
            )
            uow.commit()
        assert updated.resolution == "fixed twice"
    finally:
        database.dispose()


def test_foreign_keys_cascade_owned_records(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        session_id = new_session().id
        with database.unit_of_work() as uow:
            uow.sessions.create(new_session())
            uow.hints.record_once(session_id, 1)
            uow.retrospectives.save(session_id, RetrospectiveInput(symptom="test"))
            uow.commit()
        with database.session_factory.begin() as session:
            session.execute(delete(LabSessionRecord).where(LabSessionRecord.id == session_id))
        with database.session_factory() as session:
            assert session.scalar(select(SessionEventRecord)) is None
            assert session.scalar(select(HintUsageRecord)) is None
            assert session.scalar(select(RetrospectiveRecord)) is None
    finally:
        database.dispose()


def test_unit_of_work_methods_require_active_context(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        uow = database.unit_of_work()
        with pytest.raises(RuntimeError, match="not active"):
            uow.commit()
        with pytest.raises(RuntimeError, match="not active"):
            uow.rollback()
    finally:
        database.dispose()
