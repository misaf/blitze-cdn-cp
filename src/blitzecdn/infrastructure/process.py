from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

_LOOKUP_ERRORS = (ProcessLookupError, PermissionError)


class ThreadBackgroundRunner:
    """Runs queued work on a daemon thread. The ``BackgroundRunner`` adapter.

    Daemon threads because the work is a deployment: it holds a cross-process
    lock and writes its outcome to the database, and a controller being shut
    down mid-run must not be blocked from exiting by it. The interrupted run is
    recorded as ABANDONED by ``abandon_running`` on the next start.

    ``start`` deliberately does not catch anything ``Thread.start`` raises. The
    caller is holding the deployment lock and unwinds it on failure; see
    :class:`~blitzecdn.ports.BackgroundRunner`.
    """

    def start(self, work: Callable[[], None], *, name: str) -> None:
        threading.Thread(target=work, name=name, daemon=True).start()


def terminate_process_group(
    process: subprocess.Popen[Any],
    drain: Callable[..., Any],
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Stop a ``start_new_session`` child *and* every worker it forked.

    Killing only the direct child leaves tools that fan out across hosts —
    ansible-playbook, certbot — with orphaned workers that keep mutating remote
    state after we have already recorded the run as timed out. ``drain`` reaps
    the child; pass ``process.communicate`` when its output is piped and
    ``process.wait`` when it is not.
    """
    with suppress(*_LOOKUP_ERRORS):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        drain(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        with suppress(*_LOOKUP_ERRORS):
            os.killpg(process.pid, signal.SIGKILL)
        drain()
