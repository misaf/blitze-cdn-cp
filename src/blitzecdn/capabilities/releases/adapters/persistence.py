"""Reading and writing content-addressed releases; every write is insert-if-absent.

A row already present is identical by construction, so recompiling unchanged
state on every ``validate`` adds nothing.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from blitzecdn.capabilities.releases.adapters.tables import (
    ReleaseConfigurationRow,
    ReleaseInputsRow,
    ReleaseRow,
)
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.releases.ports import ReleaseReferences
from blitzecdn.core.domain.configuration import ConfigurationBundle
from blitzecdn.core.exceptions import ConfigurationError, NotFoundError
from blitzecdn.core.persistence.engine import Database

__all__ = ["ReleaseStore"]


def _rows_affected(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


class ReleaseStore:
    def __init__(self, database: Database, referenced: ReleaseReferences) -> None:
        self._db = database
        #: Which releases something else still needs, injected so this store never
        #: reads the deployments table.
        self._referenced = referenced

    def save(self, release: Release, inputs: ReleaseInputs) -> Release:
        """Put both rows on record, or leave the identical ones already there.

        Inputs first, for the foreign key. ``ON CONFLICT DO NOTHING`` because
        concurrent compiles of unchanged state are ordinary.
        """
        if release.inputs_digest != inputs.digest:
            raise ConfigurationError("release inputs do not match the compiled digest")
        with self._db.session() as session:
            session.execute(
                sqlite_insert(ReleaseInputsRow)
                .values(
                    digest=inputs.digest,
                    document=inputs.encode(),
                    created_at=self._db.now(),
                )
                .on_conflict_do_nothing(index_elements=[ReleaseInputsRow.digest])
            )
            session.execute(
                sqlite_insert(ReleaseRow)
                .values(
                    digest=release.digest,
                    id=release.id,
                    compiler_version=release.compiler_version,
                    inputs_digest=release.inputs_digest,
                    created_at=self._db.now(),
                    document=release.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=[ReleaseRow.digest])
            )
        return release

    def save_configuration(self, release: Release, bundle: ConfigurationBundle) -> None:
        """Record what a release rendered to, once; rollbacks ship that first render."""
        with self._db.session() as session:
            session.execute(
                sqlite_insert(ReleaseConfigurationRow)
                .values(
                    release_digest=release.digest,
                    digest=bundle.digest,
                    created_at=self._db.now(),
                    document=bundle.encode(),
                )
                .on_conflict_do_nothing(
                    index_elements=[ReleaseConfigurationRow.release_digest]
                )
            )

    def configuration(self, release_id: str) -> ConfigurationBundle:
        """The configuration tree a stored release rendered to, verified."""
        with self._db.session() as session:
            row = self._row(session, release_id)
            stored = session.get(ReleaseConfigurationRow, row.digest)
            if stored is None:
                raise NotFoundError(
                    f"release {release_id!r} has no rendered configuration on record"
                )
            try:
                bundle = ConfigurationBundle.decode(stored.document)
            except ValueError as exc:
                raise ConfigurationError(
                    f"release {release_id!r} configuration failed integrity "
                    f"verification: {exc}"
                ) from exc
            if bundle.digest != stored.digest:
                raise ConfigurationError(
                    f"release {release_id!r} configuration failed integrity "
                    "verification"
                )
            return bundle

    def get(self, release_id: str) -> Release:
        with self._db.session() as session:
            return self._release(self._row(session, release_id))

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a release was compiled from, for a rollback."""
        with self._db.session() as session:
            row = self._row(session, release_id)
            inputs = session.get(ReleaseInputsRow, row.inputs_digest)
            if inputs is None:
                # Unreachable while the foreign key holds; names the missing row if not.
                raise NotFoundError(
                    f"release {release_id!r} names inputs {row.inputs_digest!r}, "
                    "which are not on record"
                )
            decoded = ReleaseInputs.decode(inputs.document)
            if decoded.digest != row.inputs_digest:
                raise ConfigurationError(
                    f"release {release_id!r} inputs failed integrity verification"
                )
            return decoded

    def list_releases(self, limit: int | None = 20) -> list[Release]:
        with self._db.session() as session:
            rows = session.scalars(
                select(ReleaseRow)
                .order_by(col(ReleaseRow.created_at).desc())
                .limit(limit)
            ).all()
            return [self._release(row) for row in rows]

    def prune(self, keep: int) -> int:
        """Drop releases beyond the newest ``keep`` that nothing still names.

        The deployments foreign key enforces "nothing names it". Inputs are kept, since
        every release compiled from them names them.
        """
        with self._db.session() as session:
            survivors = (
                select(col(ReleaseRow.digest))
                .order_by(col(ReleaseRow.created_at).desc())
                .limit(keep)
                .scalar_subquery()
            )
            return _rows_affected(
                session.execute(
                    delete(ReleaseRow).where(
                        col(ReleaseRow.digest).not_in(survivors),
                        col(ReleaseRow.id).not_in(self._referenced.in_use()),
                    )
                )
            )

    @staticmethod
    def _row(session: Any, release_id: str) -> ReleaseRow:
        """One release by its short id or its full digest; both are printed."""
        row = session.scalars(
            select(ReleaseRow).where(
                (col(ReleaseRow.id) == release_id)
                | (col(ReleaseRow.digest) == release_id)
            )
        ).first()
        if row is None:
            raise NotFoundError(f"release {release_id!r} does not exist")
        return cast("ReleaseRow", row)

    @staticmethod
    def _release(row: ReleaseRow) -> Release:
        release = Release.model_validate(row.document)
        if (
            release.digest != row.digest
            or release.id != row.id
            or release.inputs_digest != row.inputs_digest
        ):
            raise ConfigurationError(
                f"release {row.id!r} failed integrity verification"
            )
        return release
