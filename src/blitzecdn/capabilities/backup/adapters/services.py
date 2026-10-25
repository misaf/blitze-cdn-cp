"""Guard database restore ordering under the Docker-owned runtime."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from blitzecdn.core.exceptions import ExecutionError


class ComposeRestoreGuard:
    """Require the host wrapper to have stopped persistent containers before restore.

    The container has no Docker socket; the host wrapper records, stops and
    restarts services around the restore. A source checkout needs no Docker.
    """

    @contextmanager
    def stopped(self) -> Iterator[None]:
        if (
            Path("/.dockerenv").exists()
            and os.environ.get("COMPOSE_RESTORE_OFFLINE") != "1"
        ):
            raise ExecutionError(
                "database restore must run through the host 'blitzecdn backup "
                "restore' wrapper so the host can stop the API and worker"
            )
        yield


__all__ = ["ComposeRestoreGuard"]
