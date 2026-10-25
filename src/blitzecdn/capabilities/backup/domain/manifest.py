"""What a backup is, independent of tar, SQLite, or the filesystem.

The archive format is read by other releases, possibly much later, so the
trust rules live here with no I/O: component names, the manifest, the version
gate, and the member checks applied before anything is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from blitzecdn.core.exceptions import ConfigurationError

#: The archive layout's own version, independent of the database schema;
#: they change for different reasons.
BACKUP_FORMAT_VERSION = 1

#: The authoritative component list; directories are never inferred.
MANIFEST_NAME = "manifest.json"

#: UTC, readable, no colons, `Z` marking UTC.
_FILENAME_FORMAT = "blitzecdn-backup-%Y-%m-%d_%H-%M-%SZ.tar.gz"


class BackupComponent(StrEnum):
    """State a controller cannot rebuild for itself; everything else is derived."""

    #: Zones, records, edges, fleet-wide Ansible policy, deployment snapshots
    #: and the audit trail — every canonical decision an operator has made.
    DATABASE = "database"
    #: Certificates and keys; re-issuable only at a CA round trip per site.
    TLS = "tls"
    #: The ACME account; losing it registers a new one and drops renewal history.
    ACME = "acme"
    #: Configuration, secrets and the SSH key every edge trusts.
    CONFIG = "config"


class BackupManifest(BaseModel):
    """`manifest.json`, and the compatibility rules it has to satisfy.

    ``extra="ignore"``, unlike the rest of the domain: a later release may add
    fields, and ``format_version`` is what gates compatibility.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    format_version: int
    created_at: datetime
    blitzecdn_version: str
    #: The archived database's Alembic revision, or ``None`` with no database.
    database_schema_version: str | None = None
    components: tuple[BackupComponent, ...]

    @field_validator("components")
    @classmethod
    def _normalise(
        cls, value: tuple[BackupComponent, ...]
    ) -> tuple[BackupComponent, ...]:
        if not value:
            raise ValueError("a backup must contain at least one component")
        if len(set(value)) != len(value):
            raise ValueError("components must not repeat")
        return tuple(sorted(value, key=str))

    @model_validator(mode="after")
    def _readable(self) -> Self:
        if self.format_version < 1:
            raise ValueError("format_version must be positive")
        if self.format_version > BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"backup format {self.format_version} is newer than this "
                f"installation understands ({BACKUP_FORMAT_VERSION}); "
                "upgrade BlitzeCDN and restore again"
            )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("created_at must be in UTC")
        has_database = BackupComponent.DATABASE in self.components
        if has_database and self.database_schema_version is None:
            raise ValueError("a database backup must record its schema version")
        if not has_database and self.database_schema_version is not None:
            raise ValueError(
                "database_schema_version must be null without a database component"
            )
        return self


def parse_manifest(document: object) -> BackupManifest:
    """Validate a decoded ``manifest.json``, failing with one operator-safe line."""
    if not isinstance(document, dict):
        raise ConfigurationError(f"{MANIFEST_NAME} is not a JSON object")
    try:
        return BackupManifest.model_validate(document)
    except ValueError as exc:
        raise ConfigurationError(_first_reason(exc, document)) from exc


def _first_reason(exc: ValueError, document: dict[str, Any]) -> str:
    """The one sentence an operator needs, naming an unknown component outright."""
    known = {member.value for member in BackupComponent}
    raw = document.get("components")
    if isinstance(raw, list):
        unknown = sorted(str(name) for name in raw if str(name) not in known)
        if unknown:
            return (
                f"backup contains unknown components: {', '.join(unknown)}; "
                "this installation cannot restore them"
            )
    reasons = getattr(exc, "errors", None)
    if callable(reasons):
        found = reasons()
        if found:
            return f"invalid {MANIFEST_NAME}: {found[0]['msg']}"
    return f"invalid {MANIFEST_NAME}: {exc}"


def backup_filename(moment: datetime) -> str:
    """The archive name for a backup taken at ``moment``, whatever it holds."""
    return moment.strftime(_FILENAME_FORMAT)


def unsafe_member(
    name: str, *, is_link: bool = False, link_target: str | None = None
) -> str | None:
    """Why this archive entry must not be extracted, or ``None`` if it may be.

    Absolute rules: no absolute path, traversal, root escape, or link of any kind.
    A link checked safe can be made unsafe by a later member, and backups never
    need one.
    """
    if not name or name in {".", "/"}:
        return "archive contains an entry with no name"
    if name.startswith("/") or name.startswith("\\"):
        return f"archive contains an absolute path: {name}"
    if "\\" in name:
        return f"archive contains a non-portable path separator: {name}"
    if ":" in name.split("/", 1)[0] and len(name.split("/", 1)[0]) == 2:
        return f"archive contains a drive-qualified path: {name}"
    parts = name.split("/")
    if any(part == ".." for part in parts):
        return f"archive escapes its root: {name}"
    if is_link:
        return f"archive contains a link, which is never restored: {name}"
    if link_target is not None:
        return f"archive contains a link, which is never restored: {name}"
    return None


def member_component(name: str) -> BackupComponent | None:
    """The component a member belongs to, ``None`` for the manifest; else raise."""
    if name == MANIFEST_NAME:
        return None
    root = name.split("/", 1)[0]
    try:
        return BackupComponent(root)
    except ValueError as exc:
        raise ConfigurationError(
            f"archive contains an unexpected entry: {name}"
        ) from exc


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "MANIFEST_NAME",
    "BackupComponent",
    "BackupManifest",
    "backup_filename",
    "member_component",
    "parse_manifest",
    "unsafe_member",
]
