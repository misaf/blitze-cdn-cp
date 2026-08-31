"""What a backup *is*, independent of tar, SQLite, or the filesystem.

The archive format is a compatibility contract: an archive written by one
release is read by another, possibly much later and possibly on a host that has
never run this one. So the rules that decide whether an archive can be trusted
live here, in the one layer with no I/O to hide them behind — the component
names, the manifest, the version gate, and the member-safety rules an extractor
has to apply *before* it writes anything.

Nothing here opens a file. The adapters supply names and link targets; this
decides what they mean.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from blitzecdn.core.exceptions import ConfigurationError

#: The archive layout's own version, deliberately not the database schema's.
#:
#: They move for different reasons. A migration changes what is inside
#: `database/blitzecdn.db` without changing where that file sits in the
#: archive; adding a component or renaming a directory changes the archive
#: without touching a table. Folding them into one number would force a
#: restore to reject archives it can read perfectly well.
BACKUP_FORMAT_VERSION = 1

#: The manifest is the authoritative list of what an archive holds. A restore
#: reads this and never infers components from the directories it finds, so a
#: stray directory cannot smuggle itself into a restore as a component.
MANIFEST_NAME = "manifest.json"

#: Filenames are UTC and readable, in that order of priority. Colons are not
#: portable across filesystems, hence the `-` in the time, and the trailing `Z`
#: says the timestamp is not local without needing an offset.
_FILENAME_FORMAT = "blitzecdn-backup-%Y-%m-%d_%H-%M-%SZ.tar.gz"


class BackupComponent(StrEnum):
    """The authoritative state a controller cannot rebuild for itself.

    Everything else a running controller has on disk is derived: the rendered
    desired state comes from the database, run logs are history rather than
    state, and generated edge configuration is produced by the next deploy. A
    component exists here only when losing it loses something.
    """

    #: Zones, records, edges, fleet-wide Ansible policy, deployment snapshots
    #: and the audit trail — every canonical decision an operator has made.
    DATABASE = "database"
    #: Installed certificate chains, their private keys, and the metadata file
    #: that pairs them. Re-issuable in principle, at the cost of a CA round
    #: trip per site and whatever rate limit that runs into.
    TLS = "tls"
    #: The local ACME account and its issuance history. Losing it does not stop
    #: issuance, but it registers a new account and discards the renewal
    #: bookkeeping that says what was issued for what.
    ACME = "acme"
    #: Operator configuration and the controller's own identity: the settings
    #: file, the secrets that are deliberately not valid TOML keys, and the SSH
    #: key every edge has already authorised. Regenerating the key pair means
    #: reaching every edge to install the new one — using the key you no longer
    #: have.
    CONFIG = "config"


class BackupManifest(BaseModel):
    """`manifest.json`, and the compatibility rules it has to satisfy.

    ``extra="ignore"`` rather than the ``extra="forbid"`` used everywhere else
    in the domain, and for the reason that makes this model different: every
    other model validates something this release produced, while this one
    validates something a *later* release may have produced. A future field
    that this release has no use for must not turn a readable archive into an
    unreadable one — that is what ``format_version`` is for, and it is checked
    explicitly below.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    format_version: int
    created_at: datetime
    blitzecdn_version: str
    #: The Alembic revision the archived database was at, or ``None`` when the
    #: archive holds no database. Recorded so a restore can refuse a schema
    #: this installation has never heard of instead of migrating blindly.
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
    """Validate a decoded ``manifest.json``, as an operator-safe error.

    Pydantic's own message names the offending field but reads like a stack of
    validation output; a restore that refuses an archive should say what is
    wrong with the archive in one line.
    """
    if not isinstance(document, dict):
        raise ConfigurationError(f"{MANIFEST_NAME} is not a JSON object")
    try:
        return BackupManifest.model_validate(document)
    except ValueError as exc:
        raise ConfigurationError(_first_reason(exc, document)) from exc


def _first_reason(exc: ValueError, document: dict[str, Any]) -> str:
    """Turn a validation failure into the sentence an operator needs.

    An unknown component is the case worth naming outright: it means the
    archive was written by a release that backs up something this one does not
    know how to restore, and "input should be 'acme', 'config'…" does not say
    that.
    """
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
    """The archive name for a backup taken at ``moment``.

    One name for every backup whatever it holds. A `--only tls` archive is the
    same kind of file as a full one — the manifest is what differs — so the
    name must not imply otherwise, or an operator would have to know the naming
    scheme to know what they are holding.
    """
    return moment.strftime(_FILENAME_FORMAT)


def unsafe_member(
    name: str, *, is_link: bool = False, link_target: str | None = None
) -> str | None:
    """Why this archive entry must not be extracted, or ``None`` if it may be.

    Extraction writes attacker-influenced names into a privileged directory, so
    the rules are absolute rather than best-effort: no absolute path, no
    traversal, no drive or root escape, and no link of any kind. Links are
    refused outright rather than resolved because a link that is safe when the
    archive is checked can be made unsafe by a later member that replaces the
    directory it points through — the check and the write are not atomic, and
    nothing a backup legitimately holds needs a link.
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
    """The component an archive member belongs to, or ``None`` for the manifest.

    Raises for anything else: an archive may hold the manifest and the
    directories of the components it declares, and nothing besides.
    """
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
