"""Guard database restore ordering under the Docker-owned runtime."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from blitzecdn.exceptions import ExecutionError


class ComposeRestoreGuard:
    """Require the host wrapper to stop persistent containers before restore.

    The application container deliberately has no Docker socket. Lifecycle
    ownership stays on the host, where the installed ``blitzecdn`` wrapper
    records which Compose services are running, stops them, runs the ephemeral
    restore container, and restores that exact running set in a trap.

    A source-checkout restore remains usable without Docker; only a process
    actually running in a container must prove the host established the
    offline boundary.
    """

    @contextmanager
    def stopped(self) -> Iterator[None]:
        if (
            Path("/.dockerenv").exists()
            and os.environ.get("COMPOSE_RESTORE_OFFLINE") != "1"
        ):
            raise ExecutionError(
                "database restore must run through the host 'blitzecdn backup "
                "restore' wrapper so Compose can stop the API and worker"
            )
        yield


__all__ = ["ComposeRestoreGuard"]
