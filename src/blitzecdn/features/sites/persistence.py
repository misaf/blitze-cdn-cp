"""Persistence for the derived site projection.

The rows live here rather than beside the zones that produce them because a
site is what the rest of the control plane reads: `SiteReader` is the port every
capability is handed, and this is the one adapter behind it. `dns` writes it
through its own `SiteProjection` port, which is the same object seen from the
side that derives it.
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from blitzecdn.core.database_engine import Database
from blitzecdn.core.database_models import ProjectionStateRow, SiteRow
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.features.sites.domain import CdnSite

_SITE_COLUMNS = frozenset({"name", "server_names", "origin_host"})


class SiteStore:
    """The derived virtual hosts.

    Nothing should write here except re-derivation from records: an edit made
    directly survives only until the next record change silently reverts it.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_sites(self) -> list[CdnSite]:
        with self._db.session() as session:
            rows = session.scalars(select(SiteRow).order_by(SiteRow.name)).all()
            return [self._site(row) for row in rows]

    def get_site(self, name: str) -> CdnSite:
        with self._db.session() as session:
            row = session.get(SiteRow, name)
            if row is None:
                raise NotFoundError(f"CDN site {name!r} does not exist")
            return self._site(row)

    def create_site(self, site: CdnSite) -> CdnSite:
        with self._db.session() as session:
            session.add(self._row(site))
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"CDN site {site.name!r} already exists") from exc
        return site

    def replace_site(self, site: CdnSite) -> CdnSite:
        with self._db.session() as session:
            row = session.get(SiteRow, site.name)
            if row is None:
                raise NotFoundError(f"CDN site {site.name!r} does not exist")
            self._apply(row, site)
        return site

    def delete_site(self, name: str) -> None:
        with self._db.session() as session:
            row = session.get(SiteRow, name)
            if row is None:
                raise NotFoundError(f"CDN site {name!r} does not exist")
            session.delete(row)

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        with self._db.session() as session:
            session.execute(delete(SiteRow))
            session.flush()
            session.add_all([self._row(site) for site in sites])

    def projection_revision(self) -> str | None:
        with self._db.session() as session:
            row = session.get(ProjectionStateRow, "sites")
            return row.source_revision if row else None

    def set_projection_revision(self, revision: str) -> None:
        with self._db.session() as session:
            statement = sqlite_insert(ProjectionStateRow).values(
                name="sites", source_revision=revision, projected_at=self._db.now()
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProjectionStateRow.name],
                    set_={
                        "source_revision": statement.excluded.source_revision,
                        "projected_at": statement.excluded.projected_at,
                    },
                )
            )

    def _row(self, site: CdnSite) -> SiteRow:
        row = SiteRow(name=site.name)
        self._apply(row, site)
        return row

    def _apply(self, row: SiteRow, site: CdnSite) -> None:
        row.server_names = list(site.server_names)
        row.origin_host = site.origin_host
        row.policy = site.model_dump(mode="json", exclude=set(_SITE_COLUMNS))
        row.updated_at = self._db.now()

    @staticmethod
    def _site(row: SiteRow) -> CdnSite:
        return CdnSite.model_validate(
            {
                **row.policy,
                "name": row.name,
                "server_names": tuple(row.server_names),
                "origin_host": row.origin_host,
            }
        )


__all__ = ["SiteStore"]
