"""Safe discovery and execution of local command-line tools."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kubelab.config import ToolName, ToolsConfig

MAX_CAPTURED_OUTPUT = 65_536


class ToolExecutionError(RuntimeError):
    """Raised when an external tool cannot be executed."""


class ProcessTimeoutError(ToolExecutionError):
    """Raised when an external tool exceeds its diagnostic timeout."""


class ToolSource(StrEnum):
    """Where an executable was discovered."""

    CONFIG = "config"
    PATH = "path"
    KNOWN_LOCATION = "known_location"


@dataclass(frozen=True, slots=True)
class LocatedTool:
    """A resolved executable and the non-secret source of that resolution."""

    name: ToolName
    path: Path
    source: ToolSource


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded output from one shell-free process invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


def default_linux_locations(home: Path | None = None) -> Mapping[ToolName, tuple[Path, ...]]:
    """Return common WSL Ubuntu executable locations outside the active PATH."""
    user_home = home or Path.home()
    prefixes = (
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/snap/bin"),
        user_home / ".local" / "bin",
        Path("/home/linuxbrew/.linuxbrew/bin"),
    )
    return {name: tuple(prefix / name.value for prefix in prefixes) for name in ToolName}


class ToolLocator:
    """Discover tools with deterministic, user-controllable precedence."""

    def __init__(
        self,
        config: ToolsConfig,
        *,
        which: Callable[[str], str | None] = shutil.which,
        known_locations: Mapping[ToolName, Sequence[Path]] | None = None,
    ) -> None:
        self._config = config
        self._which = which
        self._known_locations = (
            default_linux_locations() if known_locations is None else known_locations
        )

    def locate(self, name: ToolName) -> LocatedTool | None:
        """Resolve a configured, PATH, or known-location executable."""
        configured = self._config.path_for(name)
        if configured is not None and configured.is_file():
            return LocatedTool(name, configured.resolve(), ToolSource.CONFIG)

        path_match = self._which(name.value)
        if path_match:
            candidate = Path(path_match)
            if candidate.is_file():
                return LocatedTool(name, candidate.resolve(), ToolSource.PATH)

        for candidate in self._known_locations.get(name, ()):
            if candidate.is_file():
                return LocatedTool(name, candidate.resolve(), ToolSource.KNOWN_LOCATION)
        return None


class ProcessRunner:
    """Execute an argument vector without a shell and with bounded output."""

    def __init__(self, *, max_output_bytes: int = MAX_CAPTURED_OUTPUT) -> None:
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        executable: Path,
        arguments: Sequence[str],
        *,
        timeout_seconds: int = 10,
    ) -> CommandResult:
        """Run one diagnostic command, preserving nonzero exit codes."""
        args = (str(executable), *(str(argument) for argument in arguments))
        try:
            completed = subprocess.run(
                args,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_seconds,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessTimeoutError(
                f"{executable} timed out after {timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise ToolExecutionError(f"Failed to execute {executable}: {exc}") from exc

        stdout, stdout_truncated = self._truncate(completed.stdout)
        stderr, stderr_truncated = self._truncate(completed.stderr)
        return CommandResult(
            args=args,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )

    def _truncate(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= self._max_output_bytes:
            return value, False
        clipped = encoded[: self._max_output_bytes].decode("utf-8", errors="ignore")
        return f"{clipped}\n[output truncated]", True
