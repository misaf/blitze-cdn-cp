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
    """Write this run's variables to a path no other run can reach, removed after.

    Runs that skip the deployment lock overlap routinely; with a shared file a two-URL
    purge could read another run's ``purge_all: true`` and empty every cache.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{prefix}-{uuid4().hex}.yml"
    try:
        atomic_write_yaml(path, values)
        yield path
    finally:
        path.unlink(missing_ok=True)
