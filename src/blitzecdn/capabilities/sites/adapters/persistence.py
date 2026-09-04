"""Persistence for the virtual hosts.

The one adapter behind ``SiteStore`` and ``SiteReader`` alike, and — through
`dns`'s ``SiteHostnames`` — behind the one column this capability does not write.
That third audience is why ``set_server_names`` is here rather than folded into
``replace_site``: a record change must be able to update a site's hostnames
without restating its policy, and a policy change must not be able to touch its
hostnames at all.
"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from blitzecdn.capabilities.sites.adapters.tables import ProjectionStateRow, SiteRow
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.persistence.engine import Database

_SITE_COLUMNS = frozenset({"name", "server_names", "origin_host"})


class SiteStore:
    """The virtual hosts."""

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
        """Write everything but the hostnames.

        ``server_names`` is skipped rather than written from the model passed
        in: the caller holds a ``CdnSite`` read a moment ago, and writing its
        hostnames back would silently revert a record change made in between.
        The column has one writer and it is :meth:`set_server_names`.
        """
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

    def set_server_names(self, site: str, server_names: tuple[str, ...]) -> None:
        with self._db.session() as session:
            row = session.get(SiteRow, site)
            if row is None:
                raise NotFoundError(f"CDN site {site!r} does not exist")
            if tuple(row.server_names) == server_names:
                return
            row.server_names = list(server_names)
            row.updated_at = self._db.now()

    def replace_all_sites(self, sites: list[CdnSite]) -> None:
        """Restore the table wholesale. Used by rollback and backup restore.

        The only writer that does set ``server_names``, because the snapshot it
        restores holds records and sites that already agree with each other.
        """
        with self._db.session() as session:
            session.execute(delete(SiteRow))
            session.flush()
            session.add_all([self._row(site, hostnames=True) for site in sites])

    def projection_revision(self) -> str | None:
        with self._db.session() as session:
            row = session.get(ProjectionStateRow, "site_hostnames")
            return row.source_revision if row else None

    def set_projection_revision(self, revision: str) -> None:
        with self._db.session() as session:
            statement = sqlite_insert(ProjectionStateRow).values(
                name="site_hostnames",
                source_revision=revision,
                projected_at=self._db.now(),
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

    def _row(self, site: CdnSite, *, hostnames: bool = False) -> SiteRow:
        row = SiteRow(name=site.name, server_names=list(site.server_names))
        self._apply(row, site, hostnames=hostnames)
        return row

    def _apply(self, row: SiteRow, site: CdnSite, *, hostnames: bool = False) -> None:
        if hostnames:
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
