"""Fingerprinted GeoIP databases under the controller's state directory.

Each file is named by its digest and never rewritten, so a release that pinned
one can be rolled back to for as long as it is retained. ``current`` names the
newest, and is replaced atomically after the file it names is complete.
"""

from __future__ import annotations

from pathlib import Path

from blitzecdn.capabilities.security.geoip.domain import (
    GeoIpDatabase,
    database_filename,
    digest_of,
    require_database,
)
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.runtime.filesystem import atomic_write_bytes

_CURRENT = "current"


class GeoIpDatabaseStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def current_digest(self) -> str | None:
        pointer = self._directory / _CURRENT
        if not pointer.is_file():
            return None
        return pointer.read_text(encoding="ascii").strip()

    def current(self) -> GeoIpDatabase | None:
        """The database releases ship from now on, or ``None`` before the first."""
        pointer = self._directory / _CURRENT
        sha256 = self.current_digest()
        if sha256 is None:
            return None
        try:
            path = self._directory / database_filename(sha256)
        except ValueError as exc:
            raise ConfigurationError(
                f"{pointer} does not name a GeoIP database digest"
            ) from exc
        if not path.is_file():
            raise ConfigurationError(
                f"{pointer} names {path.name}, which is missing; run "
                "`blitzecdn geoip update` to download the database again"
            )
        return GeoIpDatabase(path=path, sha256=sha256)

    def install(self, payload: bytes) -> GeoIpDatabase:
        try:
            sha256 = require_database(payload)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        path = self._directory / database_filename(sha256)
        if not path.is_file():
            # Not secret: the same file is world-readable on every edge.
            atomic_write_bytes(path, payload, mode=0o644)
        atomic_write_bytes(
            self._directory / _CURRENT, f"{sha256}\n".encode("ascii"), mode=0o644
        )
        return GeoIpDatabase(path=path, sha256=sha256)

    def collect(self, keep: frozenset[str]) -> int:
        if not self._directory.is_dir():
            return 0
        removed = 0
        for path in sorted(self._directory.iterdir()):
            digest = digest_of(path.name)
            if digest is None or digest in keep or not path.is_file():
                continue
            path.unlink()
            removed += 1
        return removed
