"""Cross-platform operation lock behavior."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from kubelab.operation_lock import OperationLock, OperationLockError


def _hold_lock(path: str, ready: Any, release: Any) -> None:
    with OperationLock(Path(path), timeout_seconds=1):
        ready.set()
        release.wait(timeout=10)


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    lock = OperationLock(tmp_path / "state" / "operations.lock", timeout_seconds=0)

    assert lock.acquired is False
    with lock as acquired:
        assert acquired is lock
        assert lock.acquired is True
        assert lock.path.is_file()
    assert lock.acquired is False


def test_second_owner_times_out_without_stealing_lock(tmp_path: Path) -> None:
    path = tmp_path / "operations.lock"
    first = OperationLock(path, timeout_seconds=0)
    second = OperationLock(path, timeout_seconds=0.05)

    with first:
        with pytest.raises(OperationLockError, match="already running") as caught:
            second.acquire()
        assert caught.value.code == "OPERATION_IN_PROGRESS"
        assert first.acquired is True
        assert second.acquired is False


def test_lock_is_not_reentrant_and_release_without_acquire_is_safe(tmp_path: Path) -> None:
    lock = OperationLock(tmp_path / "operations.lock", timeout_seconds=0)
    lock.release()
    lock.acquire()
    try:
        with pytest.raises(OperationLockError, match="not reentrant"):
            lock.acquire()
    finally:
        lock.release()


def test_negative_timeout_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        OperationLock(tmp_path / "operations.lock", timeout_seconds=-1)


def test_lock_is_enforced_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    path = tmp_path / "operations.lock"
    process = context.Process(target=_hold_lock, args=(str(path), ready, release))
    process.start()
    try:
        assert ready.wait(timeout=10)
        with pytest.raises(OperationLockError):
            OperationLock(path, timeout_seconds=0.05).acquire()
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0
