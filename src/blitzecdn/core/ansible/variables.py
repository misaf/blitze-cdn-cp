"""Per-run variable files: one caller's request, visible to nobody else."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from blitzecdn.core.runtime.filesystem import atomic_write_yaml

__all__ = ["run_variables"]


@contextmanager
def run_variables(
    directory: Path, prefix: str, values: dict[str, object]
) -> Iterator[Path]:
    """Write this run's variables to a path no other run can reach.

    A fixed filename per playbook made the file shared mutable state between
    overlapping runs, and overlap is ordinary here rather than exceptional:
    purge, stats and decommission all deliberately skip the deployment lock,
    the API serves them from a thread pool, and Dramatiq jobs are
    separate processes over one state directory. Whoever wrote last won — so
    a purge of two URLs could find ``purge_all: true`` in the file by the
    time its own playbook read it, empty the cache on every edge, and still
    return, and audit, a two-URL purge.

    Removed when the run ends: these carry one caller's request, not state
    anything reads afterwards.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{prefix}-{uuid4().hex}.yml"
    try:
        atomic_write_yaml(path, values)
        yield path
    finally:
        path.unlink(missing_ok=True)
