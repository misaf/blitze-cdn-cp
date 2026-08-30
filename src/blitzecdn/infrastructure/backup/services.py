"""Taking the control plane offline for the part of a restore that needs it.

Only systemd is supported, and only when it is actually managing these units.
A developer restoring into a checkout has no units, and a restore there must
not fail because `systemctl` is missing or answers about somebody else's host.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from blitzecdn.exceptions import ExecutionError

#: Kept in step with `CONTROL_PLANE_SERVICES` in install.sh and with the
#: control-plane role's `blitzecdn_controlplane_services`. A unit added there
#: and missed here keeps a process holding the database open across a restore.
CONTROL_PLANE_UNITS: tuple[str, ...] = (
    "blitzecdn-api.service",
    "blitzecdn-worker.service",
)

_TIMEOUT_SECONDS = 60


class SystemdServiceControl:
    """Stop the units for a block, and start whichever were running before.

    The set of units to restart is what was *active*, not the full list: a
    controller with the worker deliberately masked must not come back with it
    running because a restore decided so.
    """

    def __init__(self, units: Sequence[str] = CONTROL_PLANE_UNITS) -> None:
        self._units = tuple(units)

    @contextmanager
    def stopped(self) -> Iterator[None]:
        # One decision, taken once: is systemd managing anything here at all? A
        # developer restoring into a checkout has no units, and a restore there
        # must not fail because `systemctl` is missing.
        if shutil.which("systemctl") is None:
            yield
            return
        running = [unit for unit in self._units if self._is_active(unit)]
        for unit in running:
            self._run("stop", unit)
        try:
            yield
        finally:
            # Reversed so the API comes back after the worker it dispatches to,
            # and in a `finally` so a failed restore still leaves a controller
            # that is serving rather than one that is silently down.
            for unit in reversed(running):
                self._run("start", unit)

    def _is_active(self, unit: str) -> bool:
        return self._invoke(("systemctl", "is-active", "--quiet", unit)) == 0

    def _run(self, verb: str, unit: str) -> None:
        status = self._invoke(
            ("sudo", "--non-interactive", "/usr/bin/systemctl", verb, unit)
        )
        if status != 0:
            raise ExecutionError(f"could not {verb} {unit} (systemctl exited {status})")

    @staticmethod
    def _invoke(command: tuple[str, ...]) -> int:
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed argument array
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"could not run {' '.join(command)}: {exc}") from exc
        return completed.returncode


__all__ = ["CONTROL_PLANE_UNITS", "SystemdServiceControl"]
