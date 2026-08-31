"""Creating and restoring a backup, as a sequence of decisions.

The order is the whole design. Creating stages every component into a private
directory, writes the manifest last, and only then publishes one archive under
a name nothing else holds. Restoring reads and checks the entire archive before
it writes a single byte of it anywhere real, because a restore that discovers a
problem halfway has already destroyed the state it was asked to recover.

Nothing here knows what a tar file is, how SQLite copies itself, or where a
certificate lives. Those are the ports.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from blitzecdn.core.exceptions import ConfigurationError, NotFoundError
from blitzecdn_backup.domain import (
    BACKUP_FORMAT_VERSION,
    MANIFEST_NAME,
    BackupComponent,
    BackupManifest,
    backup_filename,
    member_component,
    parse_manifest,
    unsafe_member,
)
from blitzecdn_backup.ports import (
    ArchiveGateway,
    BackupComponentGateway,
    SchemaVersions,
    ServiceControl,
    Workspace,
)


@dataclass(frozen=True)
class BackupPolicy:
    """Where backups go and what this installation calls itself."""

    #: The directory `create` writes into when no destination is given. It is
    #: made `0700` on first use: the archives in it carry private keys, and a
    #: mode on the files alone would still let anyone list what a controller
    #: holds and when it was last backed up.
    backup_dir: Path
    #: Recorded in the manifest so an archive can say which release wrote it.
    version: str


class BackupService:
    """The one backup and restore code path, whoever asked for it."""

    def __init__(
        self,
        *,
        policy: BackupPolicy,
        components: Sequence[BackupComponentGateway],
        archive: ArchiveGateway,
        schema: SchemaVersions,
        services: ServiceControl,
        workspace: Workspace,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.archive = archive
        self.schema = schema
        self.services = services
        self.workspace = workspace
        self.clock = clock or (lambda: datetime.now(UTC))
        self._components = {gateway.component: gateway for gateway in components}

    # -- Creating ------------------------------------------------------

    def create(
        self,
        *,
        only: Sequence[BackupComponent] | None = None,
        destination: Path | None = None,
    ) -> Path:
        """Write one archive and return where it landed.

        A full backup takes what is there: a controller that has never issued a
        certificate has no TLS component, and refusing to back it up until it
        does would be absurd. An explicit selection is the opposite — asking
        for `--only tls` on such a controller is a mistake worth reporting,
        because the operator believes they are protecting something.
        """
        moment = self.clock()
        selection = self._select(only)
        target = self._destination(destination, moment)
        with self.workspace.scratch("blitzecdn-backup-") as staging:
            for component in selection:
                self._components[component].export(staging / component.value)
            manifest = BackupManifest(
                format_version=BACKUP_FORMAT_VERSION,
                created_at=moment.replace(microsecond=0),
                blitzecdn_version=self.policy.version,
                database_schema_version=(
                    self.schema.current()
                    if BackupComponent.DATABASE in selection
                    else None
                ),
                components=tuple(selection),
            )
            # Written last, so an archive that has a manifest has everything
            # the manifest claims. A crash between the two leaves a staging
            # directory nothing publishes rather than an archive that lies.
            (staging / MANIFEST_NAME).write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            self.archive.write(staging, target)
        return target

    def _select(
        self, only: Sequence[BackupComponent] | None
    ) -> tuple[BackupComponent, ...]:
        if only is None:
            present = tuple(
                component
                for component in BackupComponent
                if component in self._components
                and self._components[component].present()
            )
            if not present:
                raise ConfigurationError(
                    "there is nothing to back up: no database, certificates, "
                    "ACME state, or configuration was found"
                )
            return present
        if not only:
            raise ConfigurationError("--only must name at least one component")
        repeated = sorted(
            component.value for component in set(only) if only.count(component) > 1
        )
        if repeated:
            raise ConfigurationError(
                "backup components must not repeat: " + ", ".join(repeated)
            )
        missing = [
            component.value
            for component in only
            if component not in self._components
            or not self._components[component].present()
        ]
        if missing:
            raise NotFoundError(f"nothing to back up for: {', '.join(sorted(missing))}")
        return tuple(only)

    def _destination(self, destination: Path | None, moment: datetime) -> Path:
        if destination is None:
            self.policy.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.policy.backup_dir.chmod(0o700)
        target = destination or (self.policy.backup_dir / backup_filename(moment))
        # Checked here as well as at the atomic rename. This is the message an
        # operator gets; the rename is what makes the answer true under a
        # second process racing this one.
        if target.exists():
            raise ConfigurationError(
                f"{target} already exists; refusing to overwrite a backup"
            )
        return target

    # -- Reading -------------------------------------------------------

    def inspect(self, archive: Path) -> BackupManifest:
        """What an archive holds, without extracting or restoring any of it."""
        return self._validated_manifest(archive)

    # -- Restoring -----------------------------------------------------

    def restore(self, archive: Path) -> BackupManifest:
        """Restore exactly the components the archive's manifest declares.

        The manifest is the instruction, which is why there is no `--only`
        here. A database-only archive restores a database; asking for TLS as
        well would either be ignored or invent files, and neither is an answer.
        """
        manifest = self._validated_manifest(archive)
        gateways = [self._components[name] for name in manifest.components]
        with self.workspace.scratch("blitzecdn-restore-") as staging:
            self.archive.extract(archive, staging)
            for gateway in gateways:
                gateway.validate(staging / gateway.component.value)
            self._check_staged_schema(manifest, staging)
            # Everything above this line reads. Everything below it writes.
            offline = any(gateway.requires_offline for gateway in gateways)
            with self.services.stopped() if offline else nullcontext():
                for gateway in gateways:
                    gateway.restore(staging / gateway.component.value)
        return manifest

    # -- Validation ----------------------------------------------------

    def _validated_manifest(self, archive: Path) -> BackupManifest:
        """Every check that can be made from the archive alone.

        Ordered cheapest and most fundamental first: a file that is not an
        archive should not produce a complaint about a component.
        """
        if not archive.is_file():
            raise NotFoundError(f"{archive} does not exist")
        entries = self.archive.entries(archive)
        if not entries:
            raise ConfigurationError(f"{archive} is empty")
        names: set[str] = set()
        for entry in entries:
            reason = unsafe_member(
                entry.name, is_link=entry.is_link, link_target=entry.link_target
            )
            if reason is not None:
                raise ConfigurationError(reason)
            names.add(entry.name)
        if MANIFEST_NAME not in names:
            raise ConfigurationError(f"{archive} has no {MANIFEST_NAME}")
        manifest = parse_manifest(
            self._document(self.archive.read_member(archive, MANIFEST_NAME))
        )
        self._check_components(manifest, names)
        self._check_schema(manifest)
        return manifest

    @staticmethod
    def _document(payload: bytes) -> object:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"{MANIFEST_NAME} is not valid JSON") from exc

    def _check_components(self, manifest: BackupManifest, names: set[str]) -> None:
        """The manifest and the archive have to agree, in both directions."""
        declared = set(manifest.components)
        found = {
            component
            for component in (member_component(name) for name in sorted(names))
            if component is not None
        }
        undeclared = sorted(component.value for component in found - declared)
        if undeclared:
            raise ConfigurationError(
                "archive holds data the manifest does not declare: "
                + ", ".join(undeclared)
            )
        empty = sorted(component.value for component in declared - found)
        if empty:
            raise ConfigurationError(
                "manifest declares components the archive does not contain: "
                + ", ".join(empty)
            )
        unsupported = sorted(
            component.value
            for component in declared
            if component not in self._components
        )
        if unsupported:
            raise ConfigurationError(
                "this installation cannot restore: " + ", ".join(unsupported)
            )

    def _check_schema(self, manifest: BackupManifest) -> None:
        """Refuse a database this installation could only migrate backwards.

        Alembic migrates forward from a revision it knows. A revision it has
        never heard of is a database from a *later* release, and restoring it
        would leave tables the running code cannot read while reporting
        success. There is no downgrade path here on purpose.
        """
        revision = manifest.database_schema_version
        if BackupComponent.DATABASE not in manifest.components or revision is None:
            return
        if not self.schema.known(revision):
            raise ConfigurationError(
                f"the archived database is at schema {revision}, which this "
                "installation does not know; upgrade BlitzeCDN and restore again"
            )

    def _check_staged_schema(self, manifest: BackupManifest, staging: Path) -> None:
        """Confirm the manifest describes the database actually staged."""
        if BackupComponent.DATABASE not in manifest.components:
            return
        expected = manifest.database_schema_version
        actual = self.schema.of(staging / "database" / "blitzecdn.db")
        if actual != expected:
            raise ConfigurationError(
                "the staged database schema does not match the manifest: "
                f"expected {expected}, found {actual or 'none'}"
            )


__all__ = ["BackupPolicy", "BackupService"]
