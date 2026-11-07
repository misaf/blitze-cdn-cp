"""The GeoIP database the controller holds and every release pins.

Pure values: the edition every edge resolves countries from, what a database
file is called, and the check that a download really is one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DATABASE_SHA256_VARIABLE",
    "DATABASE_SOURCE_VARIABLE",
    "EDITION",
    "MAXIMUM_DATABASE_BYTES",
    "GeoIpDatabase",
    "GeoIpRefresh",
    "database_filename",
    "digest_of",
    "require_database",
]

#: Country is all the firewall rules and the BZ-IPCountry header need;
#: GeoLite2-City is far larger and buys nothing here.
EDITION = "GeoLite2-Country"

#: The fleet variables a release pins the database by. The edge role copies the
#: file from the controller and refuses one whose digest differs.
DATABASE_SOURCE_VARIABLE = "blitzecdn_geoip_database_source"
DATABASE_SHA256_VARIABLE = "blitzecdn_geoip_database_sha256"

#: GeoLite2-Country is a few MiB. Anything near this is not that database.
MAXIMUM_DATABASE_BYTES = 128 * 1024 * 1024

#: Every MaxMind DB ends with this marker followed by its metadata section,
#: which MaxMind bounds at 128 KiB.
_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
_METADATA_WINDOW = 128 * 1024

_DATABASE_FILE = re.compile(rf"^{re.escape(EDITION)}-([0-9a-f]{{64}})\.mmdb$")


@dataclass(frozen=True, slots=True)
class GeoIpDatabase:
    """One immutable database file on the controller, named by its digest."""

    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class GeoIpRefresh:
    """What one refresh did: whether it could ask MaxMind, and what changed."""

    configured: bool
    changed: bool
    sha256: str | None
    removed: int = 0


def database_filename(sha256: str) -> str:
    """The fingerprinted name a database is stored and shipped under."""
    name = f"{EDITION}-{sha256}.mmdb"
    if _DATABASE_FILE.match(name) is None:
        raise ValueError(f"not a SHA-256 hex digest: {sha256!r}")
    return name


def digest_of(filename: str) -> str | None:
    """The digest a stored database's file name carries, or ``None``."""
    matched = _DATABASE_FILE.match(filename)
    return None if matched is None else matched.group(1)


def require_database(payload: bytes) -> str:
    """The SHA-256 of ``payload``, refused unless it is a MaxMind database.

    A proxy's error page or a truncated transfer must never become the file
    every edge resolves countries from.
    """
    if not payload:
        raise ValueError("the GeoIP database is empty")
    if len(payload) > MAXIMUM_DATABASE_BYTES:
        raise ValueError("the GeoIP database exceeds 128 MiB")
    if _METADATA_MARKER not in payload[-_METADATA_WINDOW:]:
        raise ValueError("the download is not a MaxMind database")
    return hashlib.sha256(payload).hexdigest()
