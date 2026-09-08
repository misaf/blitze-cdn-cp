"""Guard database restore ordering under the Docker-owned runtime."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from blitzecdn.core.exceptions import ExecutionError


class ComposeRestoreGuard:
    """Require the host wrapper to stop persistent containers before restore.

    The application container deliberately has no Docker socket. Lifecycle
    ownership stays on the host, where the installed ``blitzecdn`` wrapper
    uses the Docker SDK to record which services are running, stop them, run the
    ephemeral restore container, and recover that running set in a finally block.

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
                "restore' wrapper so the host can stop the API and worker"
            )
        yield


__all__ = ["ComposeRestoreGuard"]
