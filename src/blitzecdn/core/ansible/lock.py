"""The cross-process lock that serialises convergence.

One deployment at a time, fleet-wide, enforced by an advisory lock on a file
under the state directory rather than by anything in-process: the API, a CLI
``blitzecdn deploy`` and a Dramatiq worker are three processes over one state
directory, so a mutex would serialise nothing.
"""

from __future__ import annotations

import fcntl
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

from blitzecdn.core.exceptions import DeploymentBusyError

__all__ = ["DeploymentLock"]


class DeploymentLock(AbstractContextManager["DeploymentLock"]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: object | None = None

    def __enter__(self) -> DeploymentLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise DeploymentBusyError("another deployment is already running") from exc
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            stream = self._stream
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]
            self._stream = None
