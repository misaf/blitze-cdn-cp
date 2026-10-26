"""The worker entry point: a loop over the durable job table.

Builds its services, registers schedules, installs signal handlers so a stop
finishes the job in hand, and loops. No broker: the queue is rows.
"""

from __future__ import annotations

import logging

from blitzecdn.core.config import Settings

_LOGGER = logging.getLogger(__name__)

__all__ = ["main", "run_worker"]


def run_worker(settings: Settings | None = None) -> None:
    """Run the worker until asked to stop; startup is ``Worker.start``'s."""
    from blitzecdn.composition import Worker, worker_container

    resolved = settings or Settings.from_environment()
    container = worker_container(resolved)
    try:
        worker = container.get(Worker)
        try:
            worker.start()
            _LOGGER.info(
                "worker ready: %d scheduled job(s) registered",
                len(worker.scheduler.schedules.list_schedules()),
            )
            worker.runner.install_signal_handlers()
            worker.runner.run_forever()
        finally:
            worker.stop()
    finally:
        container.close()


def main() -> None:  # pragma: no cover - the process entry point
    logging.basicConfig(level=logging.INFO)
    run_worker()


if __name__ == "__main__":  # pragma: no cover - module execution
    main()
