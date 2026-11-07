"""The last blocked preflight of each site still waiting for a certificate."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import delete, select

from blitzecdn.capabilities.tls.adapters.tables import PreflightAttemptRow
from blitzecdn.capabilities.tls.certificates.domain import PreflightAttempt
from blitzecdn.core.persistence.engine import Database

__all__ = ["PreflightAttemptStore"]


class PreflightAttemptStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, site: str) -> PreflightAttempt | None:
        with self._db.session() as session:
            row = session.get(PreflightAttemptRow, site)
            return None if row is None else _attempt(row)

    def list_all(self) -> list[PreflightAttempt]:
        with self._db.session() as session:
            rows = session.scalars(
                select(PreflightAttemptRow).order_by(PreflightAttemptRow.site)
            )
            return [_attempt(row) for row in rows]

    def record(self, attempt: PreflightAttempt) -> None:
        with self._db.transaction(), self._db.session() as session:
            row = session.get(PreflightAttemptRow, attempt.site)
            if row is None:
                session.add(PreflightAttemptRow(**attempt.model_dump()))
                return
            row.fingerprint = attempt.fingerprint
            row.failures = attempt.failures
            row.checked_at = attempt.checked_at
            row.next_check_at = attempt.next_check_at
            row.summary = attempt.summary

    def clear(self, site: str) -> None:
        with self._db.transaction(), self._db.session() as session:
            session.execute(
                delete(PreflightAttemptRow).where(PreflightAttemptRow.site == site)
            )

    def retain(self, sites: Collection[str]) -> None:
        """Forget every site not named, such as one deleted or now certified."""
        with self._db.transaction(), self._db.session() as session:
            session.execute(
                delete(PreflightAttemptRow).where(
                    PreflightAttemptRow.site.not_in(list(sites))
                )
            )


def _attempt(row: PreflightAttemptRow) -> PreflightAttempt:
    return PreflightAttempt(
        site=row.site,
        fingerprint=row.fingerprint,
        failures=row.failures,
        checked_at=row.checked_at,
        next_check_at=row.next_check_at,
        summary=row.summary,
    )
