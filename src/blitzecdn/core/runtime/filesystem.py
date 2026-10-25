from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from blitzecdn.core.exceptions import ConfigurationError


def atomic_write_yaml(
    path: Path, payload: dict[str, Any], *, mode: int = 0o600
) -> None:
    """Atomically replace a YAML file, through :func:`atomic_write_bytes`."""
    document = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    atomic_write_bytes(path, document.encode("utf-8"), mode=mode)


def read_log_tail(path: Path | str | None, *, limit: int) -> str:
    """Return the last ``limit`` bytes of a run log, for a person to read.

    Nothing branches on this, so a missing log degrades to a note, not an error.
    """
    if path is None:
        return ""
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        with candidate.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
            data = stream.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").strip()
    if size > limit and text:
        return f"[earlier output omitted; full log at {candidate}]\n{text}"
    return text


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Atomically write sensitive bytes without following a destination symlink.

    Created at ``mode`` before writing, fsynced, renamed over the destination, then
    the directory fsynced: a reader sees the old file or the whole new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ConfigurationError(f"refusing to replace symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
