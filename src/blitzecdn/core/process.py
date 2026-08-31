from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress
from typing import Any

_LOOKUP_ERRORS = (ProcessLookupError, PermissionError)


def terminate_process_group(
    process: subprocess.Popen[Any],
    drain: Callable[..., Any],
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Stop a ``start_new_session`` child *and* every worker it forked.

    Killing only the direct child leaves tools such as certbot with orphaned
    workers that keep mutating remote state after we have already recorded the
    run as timed out. ``drain`` reaps
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
