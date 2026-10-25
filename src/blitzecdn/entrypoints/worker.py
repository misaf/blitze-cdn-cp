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
    """Run the worker until asked to stop; startup is ``WorkerServices.start``'s."""
    from blitzecdn.composition import Infrastructure, build_services, build_worker

    resolved = settings or Settings.from_environment()
    worker = build_worker(build_services(Infrastructure.build(resolved)))
    try:
        worker.start()
        _LOGGER.info(
            "worker ready: %d scheduled job(s) registered",
            len(worker.job_scheduler.schedules.list_schedules()),
        )
        runner = worker.job_runner
        runner.install_signal_handlers()
        runner.run_forever()
    finally:
        try:
            worker.stop()
        finally:
            worker.close()


def main() -> None:  # pragma: no cover - the process entry point
    logging.basicConfig(level=logging.INFO)
    run_worker()


if __name__ == "__main__":  # pragma: no cover - module execution
    main()
