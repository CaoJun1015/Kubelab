"""SQLite, Alembic, Repository, and UnitOfWork integration tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
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
    GuidedLearningStateRecord,
    HintUsageRecord,
    LabSessionRecord,
    RetrospectiveRecord,
    SessionEventRecord,
    SessionEvidenceSnapshotRecord,
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


def create_rich_0002_database(path: Path) -> Database:
    """Create a realistic M5 database containing every persisted Session child."""
    path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(path, lock_path=path.parent / "operations.lock", lock_timeout_seconds=0)
    command.upgrade(database._alembic_config(), "0002_guided_learning")
    session_id = new_session().id
    run_id = "00000000-0000-4000-8000-000000000101"
    timestamp = "2026-08-29 08:00:00.000000"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO lab_session "
            "(id, lab_id, namespace, status, context_name, context_fingerprint, created_at, "
            "started_at, completed_at, reset_count, last_error_code, last_error_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "lab-013-service-target-port",
                "kubelab-lab-013",
                "completed",
                "minikube",
                "a" * 64,
                timestamp,
                timestamp,
                timestamp,
                2,
                "SAFE_ERROR",
                json.dumps({"operation": "verify"}),
            ),
        )
        connection.execute(
            "INSERT INTO session_event "
            "(session_id, event_type, from_status, to_status, context, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "success_contract_passed",
                "in_progress",
                "passed",
                json.dumps({"public": True}),
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO verification_run "
            "(id, session_id, purpose, status, reset_sequence, checked_at, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, session_id, "manual", "passed", 2, timestamp, 42),
        )
        connection.execute(
            "INSERT INTO check_result "
            "(run_id, check_id, check_type, status, expected, actual, message, retryable, "
            "duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "endpoint-ready",
                "endpoint_count",
                "passed",
                json.dumps({"minimum": 1}),
                json.dumps({"count": 1}),
                "public result",
                False,
                12,
            ),
        )
        connection.execute(
            "INSERT INTO hint_usage (session_id, level, used_at, request_count) "
            "VALUES (?, ?, ?, ?)",
            (session_id, 2, timestamp, 3),
        )
        connection.execute(
            "INSERT INTO retrospective "
            "(session_id, symptom, impact, investigation, root_cause, resolution, prevention, "
            "interview_summary, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "symptom",
                "impact",
                "investigation",
                "root cause",
                "resolution",
                "prevention",
                "summary",
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE guided_learning_state SET onboarding_completed_at = ?, last_checked_at = ?, "
            "last_environment_status = ?, last_environment_report = ? WHERE id = 1",
            (timestamp, timestamp, "ready", json.dumps({"status": "ready"})),
        )
        connection.execute(
            "INSERT INTO session_evidence_snapshot "
            "(session_id, trigger, capture_status, summary, captured_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, "verify", "captured", json.dumps({"pods": 1}), timestamp),
        )
    return database


def test_initialize_creates_all_tables_and_required_pragmas(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            "alembic_version",
            "check_result",
            "guided_learning_state",
            "hint_usage",
            "lab_session",
            "retrospective",
            "session_event",
            "session_evidence_snapshot",
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
        assert revision == "0003_lab_variants"
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


def test_v010_database_upgrades_without_losing_session_history(tmp_path: Path) -> None:
    """A real 0001 database must retain history and mark an existing user as onboarded."""
    path = tmp_path / "state" / "kubelab.db"
    path.parent.mkdir(parents=True)
    database = Database(path, lock_path=path.parent / "operations.lock", lock_timeout_seconds=0)
    command.upgrade(database._alembic_config(), "0001_initial_persistence")
    created_at = "2026-08-27 00:00:00.000000"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO lab_session "
            "(id, lab_id, namespace, status, context_name, context_fingerprint, created_at, "
            "reset_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_session().id,
                "database-lab",
                "kubelab-database-lab",
                "completed",
                "minikube",
                "a" * 64,
                created_at,
                0,
            ),
        )

    database.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT lab_id FROM lab_session").fetchone() == ("database-lab",)
        state = connection.execute(
            "SELECT onboarding_completed_at, last_environment_report "
            "FROM guided_learning_state WHERE id = 1"
        ).fetchone()
        assert state is not None and state[0] is not None and state[1] is None
        assert connection.execute("SELECT request_count FROM hint_usage").fetchall() == []
        assert connection.execute("SELECT variant_id FROM lab_session").fetchone() == ("baseline",)
    assert database.backup_path.is_file()
    database.dispose()


def test_m5_database_upgrade_preserves_all_guided_learning_records(tmp_path: Path) -> None:
    path = tmp_path / "state" / "kubelab.db"
    database = create_rich_0002_database(path)

    database.initialize()

    expected_counts = {
        "lab_session": 1,
        "session_event": 1,
        "verification_run": 1,
        "check_result": 1,
        "hint_usage": 1,
        "retrospective": 1,
        "session_evidence_snapshot": 1,
        "guided_learning_state": 1,
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0003_lab_variants",
        )
        assert {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in expected_counts
        } == expected_counts
        assert connection.execute(
            "SELECT variant_id, reset_count, last_error_code FROM lab_session"
        ).fetchone() == ("baseline", 2, "SAFE_ERROR")
        assert connection.execute("SELECT level, request_count FROM hint_usage").fetchone() == (
            2,
            3,
        )
        assert connection.execute(
            "SELECT trigger, capture_status FROM session_evidence_snapshot"
        ).fetchone() == ("verify", "captured")
        assert connection.execute("SELECT symptom, root_cause FROM retrospective").fetchone() == (
            "symptom",
            "root cause",
        )
        indexes = {row[1] for row in connection.execute("PRAGMA index_list('lab_session')")}
        assert "ix_lab_session_lab_variant_created" in indexes

    assert database.backup_path.is_file()
    with sqlite3.connect(database.backup_path) as backup:
        assert backup.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_guided_learning",
        )
        assert "variant_id" not in {
            row[1] for row in backup.execute("PRAGMA table_info('lab_session')")
        }
        assert {
            table: backup.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in expected_counts
        } == expected_counts
    database.dispose()


def test_failed_m5_upgrade_keeps_original_and_checkpointed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state" / "kubelab.db"
    database = create_rich_0002_database(path)

    def fail_after_partial_schema(config: Any, revision: str) -> None:
        assert revision == "head"
        connection = config.attributes["connection"]
        connection.exec_driver_sql("CREATE TABLE partial_migration (id INTEGER)")
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(command, "upgrade", fail_after_partial_schema)

    with pytest.raises(DatabaseError, match="initialize"):
        database.initialize()

    for candidate in (path, database.backup_path):
        assert candidate.is_file()
        with sqlite3.connect(candidate) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "0002_guided_learning",
            )
            assert connection.execute("SELECT COUNT(*) FROM lab_session").fetchone() == (1,)
            assert "partial_migration" not in {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
            }
    database.dispose()


def test_upgrade_verifier_uses_copy_and_reports_only_safe_metadata(tmp_path: Path) -> None:
    path = tmp_path / "private-user" / "kubelab.db"
    database = create_rich_0002_database(path)
    database.dispose()
    source_bytes = path.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "verify_database_upgrade.py"),
            "--source",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "backup_created": True,
        "preserved_table_count": 8,
        "session_count": 1,
        "source_revision": "0002_guided_learning",
        "source_unchanged": True,
        "target_revision": "0003_lab_variants",
    }
    assert path.read_bytes() == source_bytes
    assert str(path) not in result.stdout
    assert "root cause" not in result.stdout
    assert path.with_name("kubelab.db.bak").exists() is False


def test_upgrade_verifier_ignores_transient_sqlite_shared_memory(tmp_path: Path) -> None:
    from scripts.verify_database_upgrade import _source_signatures

    path = tmp_path / "kubelab.db"
    path.write_bytes(b"durable database")
    shared_memory = path.with_name("kubelab.db-shm")
    shared_memory.write_bytes(b"lock state one")

    before = _source_signatures(path)
    shared_memory.write_bytes(b"lock state two")

    assert _source_signatures(path) == before


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


def test_guided_learning_state_hint_counts_and_evidence_are_persisted_safely(
    tmp_path: Path,
) -> None:
    """M5 state should count requests while redacting cached reports and evidence."""
    database = make_database(tmp_path)
    try:
        session_id = new_session().id
        with database.unit_of_work() as uow:
            uow.sessions.create(new_session())
            first = uow.hints.record_request(session_id, 1)
            repeated = uow.hints.record_request(session_id, 1)
            state = uow.guided_learning.save_environment_report(
                status="ready",
                report={"status": "ready", "token": "environment-secret"},
                checked_at=datetime(2026, 8, 29, 1, 0, tzinfo=UTC),
            )
            evidence = uow.guided_learning.add_evidence(
                session_id,
                trigger="verification_completed",
                capture_status="captured",
                summary={"pods": {"ready": 1}, "secret": "hidden"},
                captured_at=datetime(2026, 8, 29, 1, 1, tzinfo=UTC),
            )
            uow.commit()

        assert first == (True, 1, 1)
        assert repeated == (False, 2, 1)
        assert state.onboarding_completed_at == datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
        assert state.last_environment_report == {"status": "ready", "token": "[REDACTED]"}
        assert evidence.summary == {"pods": {"ready": 1}, "secret": "[REDACTED]"}
        with database.session_factory() as session:
            hint = session.scalar(select(HintUsageRecord))
            assert hint is not None and hint.request_count == 2
            assert session.scalar(select(GuidedLearningStateRecord)) is not None
            assert session.scalar(select(SessionEvidenceSnapshotRecord)) is not None
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
            assert session.scalar(select(SessionEvidenceSnapshotRecord)) is None
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
