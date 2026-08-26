"""Cross-process serialization for KubeLab state-changing operations."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

import portalocker


class OperationLockError(RuntimeError):
    """Raised when another process owns the global KubeLab operation lock."""

    code = "OPERATION_IN_PROGRESS"


class OperationLock:
    """Non-reentrant exclusive file lock with an explicit bounded timeout."""

    def __init__(self, path: Path, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._lock: portalocker.Lock | None = None
        self._handle: object | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.acquired:
            raise OperationLockError("Operation lock is not reentrant.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(
            str(self.path),
            mode="a",
            timeout=self.timeout_seconds,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        try:
            handle = lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise OperationLockError("Another KubeLab operation is already running.") from exc
        self._lock = lock
        self._handle = handle
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def release(self) -> None:
        if self._lock is None:
            return
        self._lock.release()
        self._lock = None
        self._handle = None

    def __enter__(self) -> OperationLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["OperationLock", "OperationLockError"]
