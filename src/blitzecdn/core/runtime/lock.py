"""The cross-process lock that serialises convergence.

An advisory file lock under the state directory, because the API, the CLI and
the worker are separate processes over one state directory.

Entering never waits. A refused entry leaves a marker beside the lock, so a
holder that can stop early — the drift check, between batches — knows someone
is waiting and can let them in. The marker is cleared whenever the lock is taken.
"""

from __future__ import annotations

import fcntl
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from types import TracebackType
from typing import TextIO

from blitzecdn.core.exceptions import DeploymentBusyError

__all__ = ["DeploymentLock"]


class DeploymentLock(AbstractContextManager["DeploymentLock"]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._wanted = path.with_name(f"{path.name}.wanted")
        self._stream: TextIO | None = None

    def __enter__(self) -> DeploymentLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            self._request()
            raise DeploymentBusyError("another deployment is already running") from exc
        self._stream = stream
        # Whoever asked has it now, or will ask again.
        self._wanted.unlink(missing_ok=True)
        return self

    def contended(self) -> bool:
        """Whether someone was refused this lock since it was taken."""
        return self._wanted.exists()

    def _request(self) -> None:
        """Say that someone is waiting; a best effort, never the reason to fail."""
        with suppress(OSError):
            self._wanted.touch(mode=0o600)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            stream = self._stream
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
            self._stream = None
