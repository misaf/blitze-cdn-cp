"""Creating and restoring a backup, as a sequence of decisions.

Creation stages everything privately, writes the manifest last, then publishes
under a fresh name. Restoration checks the whole archive before writing a byte
anywhere real. Retention removes only archives this capability named.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from blitzecdn.capabilities.backup.domain import (
    BACKUP_FORMAT_VERSION,
    MANIFEST_NAME,
    BackupComponent,
    BackupManifest,
    BackupRecord,
    backup_filename,
    backup_moment,
    member_component,
    parse_manifest,
    unsafe_member,
)
from blitzecdn.capabilities.backup.ports import (
    ArchiveGateway,
    BackupComponentGateway,
    BackupHistory,
    RestoreRecovery,
    SchemaVersions,
    ServiceControl,
    Workspace,
)
from blitzecdn.core.exceptions import ConfigurationError, NotFoundError
from blitzecdn.core.ports.clock import system_clock


@dataclass(frozen=True)
class BackupPolicy:
    """Where backups go and what this installation calls itself."""

    #: Default destination, made `0700` so archive names and dates stay private.
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
        recovery: RestoreRecovery,
        history: BackupHistory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.archive = archive
        self.schema = schema
        self.services = services
        self.workspace = workspace
        self.recovery = recovery
        self.history = history
        self.clock = clock or system_clock
        self._components = {gateway.component: gateway for gateway in components}

    # -- Creating ------------------------------------------------------

    def create(
        self,
        *,
        only: Sequence[BackupComponent] | None = None,
        destination: Path | None = None,
    ) -> Path:
        """Write one archive and return where it landed.

        A full backup takes whatever exists; an explicit selection of a component
        that does not exist is an error. Only a full backup is recorded as the
        latest, since only a full backup can rebuild the controller.
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
            # Written last, so a manifest implies everything it claims is present.
            (staging / MANIFEST_NAME).write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            self.archive.write(staging, target)
        if only is None:
            self.history.record(
                BackupRecord(
                    completed_at=self.clock().replace(microsecond=0),
                    archive=target.name,
                    size_bytes=target.stat().st_size,
                    components=selection,
                )
            )
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
        # The operator-facing check; the atomic rename is what enforces it.
        if target.exists():
            raise ConfigurationError(
                f"{target} already exists; refusing to overwrite a backup"
            )
        return target

    # -- Retaining -----------------------------------------------------

    def prune(self, *, keep: int) -> tuple[Path, ...]:
        """Delete all but the newest ``keep`` archives in the backup directory.

        Only names :func:`backup_filename` produces are candidates, so anything
        else an operator keeps there, or an archive written with ``--output``
        under its own name, is never touched. Zero keeps everything.
        """
        directory = self.policy.backup_dir
        if keep == 0 or not directory.is_dir():
            return ()
        archives = sorted(
            (
                (moment, path)
                for path in directory.iterdir()
                if (moment := backup_moment(path.name)) is not None
                and not path.is_symlink()
                and path.is_file()
            ),
            reverse=True,
        )
        expired = tuple(path for _, path in archives[keep:])
        for path in expired:
            path.unlink(missing_ok=True)
        return expired

    # -- Reading -------------------------------------------------------

    def latest(self) -> BackupRecord | None:
        """The last full backup that completed, if one has been recorded."""
        return self.history.latest()

    def inspect(self, archive: Path) -> BackupManifest:
        """What an archive holds, without extracting or restoring any of it."""
        return self._validated_manifest(archive)

    # -- Restoring -----------------------------------------------------

    def restore(self, archive: Path) -> BackupManifest:
        """Restore exactly the components the archive's manifest declares."""
        manifest = self._validated_manifest(archive)
        gateways = [self._components[name] for name in manifest.components]
        with self.workspace.scratch("blitzecdn-restore-") as staging:
            self.archive.extract(archive, staging)
            for gateway in gateways:
                gateway.validate(staging / gateway.component.value)
            self._check_staged_schema(manifest, staging)
            if BackupComponent.DATABASE in manifest.components:
                # Migrate and verify the private copy before replacing anything live.
                self.schema.migrate(staging / "database" / "blitzecdn.db")
            # Everything above changes staging only. Live writes start here.
            offline = any(gateway.requires_offline for gateway in gateways)
            with self.services.stopped() if offline else nullcontext():
                paths = [
                    path for gateway in gateways for path in gateway.recovery_paths()
                ]
                with self.recovery.protect(paths):
                    for gateway in gateways:
                        gateway.restore(staging / gateway.component.value)
        return manifest

    # -- Validation ----------------------------------------------------

    def _validated_manifest(self, archive: Path) -> BackupManifest:
        """Every check the archive alone allows, most fundamental first."""
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
        """Refuse a database from a later release; there is no downgrade path."""
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
