"""The worker entry point: a loop over the durable job table.

This module is what ``blitzecdn worker`` and the container's worker service
run. It is an entry point in exactly the sense the CLI and the API are: it
builds a control plane and calls a service on it, and it is allowed to know
both halves because nothing imports it back.

There are no actors here and no broker to connect to. Work is rows in the
control plane's own database, so the process that enqueues and the process that
consumes share a table rather than a message bus — see
``docs/decisions/0008-durable-work-on-the-primary-database.md`` for why that
turned out to be the smaller thing rather than the cruder one.

What is left in this file is the process lifecycle: build, register the
schedules this installation's plugins contribute, install signal handlers so a
container stop finishes the job in hand, and loop.
"""

from __future__ import annotations

import logging

from blitzecdn.core.config import Settings
from blitzecdn.core.plugins import ProcessKind

_LOGGER = logging.getLogger(__name__)

__all__ = ["main", "run_worker"]


def run_worker(settings: Settings | None = None) -> None:
    """Run the worker until it is asked to stop.

    Registering the schedules this installation contributes is the `jobs`
    plugin's startup contribution rather than a line here, for the reason every
    other startup contribution is one: what a process owes when it starts is
    the plugin's business, and ``RuntimeContext.process`` is how it knows this
    is the worker.
    """
    from blitzecdn.composition import build_control_plane

    resolved = settings or Settings.from_environment()
    control = build_control_plane(resolved, process=ProcessKind.WORKER)
    try:
        control.start()
        _LOGGER.info(
            "worker ready: %d scheduled job(s) registered",
            len(control.job_scheduler.schedules.list_schedules()),
        )
        runner = control.job_runner
        runner.install_signal_handlers()
        runner.run_forever()
    finally:
        try:
            control.stop()
        finally:
            control.close()


def main() -> None:  # pragma: no cover - the process entry point
    logging.basicConfig(level=logging.INFO)
    run_worker()


if __name__ == "__main__":  # pragma: no cover - module execution
    main()
