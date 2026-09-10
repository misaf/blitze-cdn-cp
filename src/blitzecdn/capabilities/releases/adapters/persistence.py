"""Reading and writing content-addressed releases.

Every write here is an insert-if-absent. A release is named by the digest of
its own contents, so a row that is already there is already identical, and
"save" means "make sure this is on record" rather than "record this again".
That is what makes recompiling free: `blitzecdn validate` compiles the current
state on every invocation, and a store that appended would turn a habit into a
history table full of the same document.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Result, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from blitzecdn.capabilities.releases.adapters.tables import (
    ReleaseInputsRow,
    ReleaseRow,
)
from blitzecdn.capabilities.releases.domain import Release, ReleaseInputs
from blitzecdn.capabilities.releases.ports import ReleaseReferences
from blitzecdn.core.exceptions import NotFoundError
from blitzecdn.core.persistence.engine import Database

__all__ = ["ReleaseStore"]


def _rows_affected(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


class ReleaseStore:
    def __init__(self, database: Database, referenced: ReleaseReferences) -> None:
        self._db = database
        #: Which releases something else still needs. Injected rather than
        #: queried: the answer lives in the deployments table, and a store that
        #: reached across to read it would be this capability holding an
        #: opinion about another one's schema — the cross-module table access
        #: the module boundaries exist to refuse.
        self._referenced = referenced

    def save(self, release: Release, inputs: ReleaseInputs) -> Release:
        """Put both rows on record, or leave the identical ones already there.

        The inputs go in first, because the release row's foreign key names
        them. Both use ``ON CONFLICT DO NOTHING`` rather than a read-then-write:
        two processes compiling the same unchanged state at the same moment is
        the ordinary case — a scheduled drift check and an operator's
        ``validate`` — and a check followed by an insert would make one of them
        fail on a row that says exactly what it was about to write.
        """
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

    def get(self, release_id: str) -> Release:
        with self._db.session() as session:
            return self._release(self._row(session, release_id))

    def inputs_for(self, release_id: str) -> ReleaseInputs:
        """The canonical state a release was compiled from, for a rollback."""
        with self._db.session() as session:
            row = self._row(session, release_id)
            inputs = session.get(ReleaseInputsRow, row.inputs_digest)
            if inputs is None:
                # Only reachable if pruning ever removed inputs a release still
                # names. It does not — the foreign key forbids it — so this
                # says which of the two rows went missing rather than raising
                # an AttributeError two frames further on.
                raise NotFoundError(
                    f"release {release_id!r} names inputs {row.inputs_digest!r}, "
                    "which are not on record"
                )
            return ReleaseInputs.decode(inputs.document)

    def list_releases(self, limit: int = 20) -> list[Release]:
        with self._db.session() as session:
            rows = session.scalars(
                select(ReleaseRow)
                .order_by(col(ReleaseRow.created_at).desc())
                .limit(limit)
            ).all()
            return [self._release(row) for row in rows]

    def prune(self, keep: int) -> int:
        """Drop releases beyond the newest ``keep`` that nothing still names.

        The foreign key from ``deployments`` is what "nothing still names"
        means, and it is enforced rather than consulted: a release a deployment
        converged is the fleet's way back, and this must not be able to remove
        it however old it is. Inputs are left alone — they are named by every
        release compiled from them, and dropping one would strand the rest.
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
        """One release by its short id, or by its full digest.

        Both, because both are printed: the short form is what a report and a
        deployment row carry, and the full digest is what a comparison quotes.
        Accepting either means an operator who copied the wrong one still gets
        their release rather than a "does not exist" about a string that plainly
        does.
        """
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
        return Release.model_validate(row.document)
