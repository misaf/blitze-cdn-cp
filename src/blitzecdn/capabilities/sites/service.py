"""Creating, changing and removing the virtual hosts.

This service exists because sites became canonical. There was nothing to put in
it while a site was a projection of a DNS record: the operations were
``record create`` and ``record update``, and the site followed. Now a site is
the thing an operator configures and a record is what points a hostname at it,
so the operations are here and the record editor is the smaller of the two.

What is deliberately *not* here is ``server_names``. `dns` maintains it from
the records routed to each site, and neither :meth:`create_site` nor
:meth:`update_site` can write it — a site is created with no hostnames and
becomes reachable when the first record names it.
"""

from __future__ import annotations

from blitzecdn.capabilities.sites.domain import CdnSite, SitePatch
from blitzecdn.capabilities.sites.ports import SiteStore
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    SslAutomaticMode,
    SslMode,
    managed_certificate_paths,
)
from blitzecdn.core.domain.events import domain_event
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["SiteService"]


class SiteService:
    """The site editor. Every other service reads what this one writes."""

    def __init__(
        self,
        *,
        sites: SiteStore,
        events: EventRecorder,
        uow: UnitOfWork,
    ) -> None:
        self.sites = sites
        self.events = events
        self.uow = uow

    # -- Reading -------------------------------------------------------

    def list_sites(self) -> list[CdnSite]:
        return self.sites.list_sites()

    def get_site(self, name: str) -> CdnSite:
        return self.sites.get_site(name)

    # -- Writing -------------------------------------------------------

    def create_site(self, site: CdnSite, operator: str) -> CdnSite:
        """Add a site. It serves nothing until a record routes a hostname here.

        Any ``server_names`` on the model passed in are dropped rather than
        rejected: the field is on ``CdnSite`` because the edge document needs
        it, not because a caller may set it, and a caller that read a site and
        wrote it back would otherwise be refused for carrying a value it never
        chose.
        """
        site = site.model_copy(update={"server_names": ()})
        with self.uow.transaction():
            created = self.sites.create_site(site)
            self.events.record(
                domain_event(
                    operator,
                    "site.created",
                    "site",
                    created.name,
                    {"origin_host": created.origin_host},
                )
            )
        return created

    def update_site(self, name: str, patch: SitePatch, operator: str) -> CdnSite:
        current = self.sites.get_site(name)
        changes = patch.model_dump(exclude_unset=True)
        updated = CdnSite.model_validate({**current.model_dump(), **changes})
        with self.uow.transaction():
            saved = self.sites.replace_site(updated)
            self.events.record(
                domain_event(
                    operator,
                    "site.updated",
                    "site",
                    saved.name,
                    {"fields": sorted(changes)},
                )
            )
        return saved

    def delete_site(self, name: str, operator: str) -> None:
        """Remove a site that no hostname still routes to.

        Refused while records point here, and the refusal names them. Deleting
        the site and leaving the records would either orphan them or silently
        unproxy hostnames the operator did not mention — so the order is theirs
        to choose, and the message says which records to deal with.
        """
        site = self.sites.get_site(name)
        if site.server_names:
            raise ConflictError(
                f"CDN site {name!r} still answers for "
                + ", ".join(repr(item) for item in site.server_names)
                + ". Repoint or delete those records first."
            )
        with self.uow.transaction():
            self.sites.delete_site(name)
            self.events.record(domain_event(operator, "site.deleted", "site", name))

    # -- Certificate state ---------------------------------------------
    #
    # Both of these used to be methods on the *zone* editor, writing certificate
    # state onto a DNS record because the derived site could not hold anything.
    # They are ordinary site updates now.

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite:
        """Point a site at the managed certificate paths for its name."""
        certificate_path, certificate_key_path = managed_certificate_paths(site.name)
        current = self.sites.get_site(site.name)
        updated = CdnSite.model_validate(
            {
                **current.model_dump(),
                "certificate_mode": mode,
                "certificate_path": certificate_path,
                "certificate_key_path": certificate_key_path,
            }
        )
        with self.uow.transaction():
            return self.sites.replace_site(updated)

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None:
        """Persist an upgrade only while the site remains enrolled in Auto.

        The checks happen outside this service, but the decision is re-checked
        against canonical state at write time. An operator opting out or
        choosing an equal/stronger mode while a scan is running therefore wins.
        """
        current = self.sites.get_site(site_name)
        if current.ssl_automatic_mode is SslAutomaticMode.CUSTOM:
            return None
        if target.security_rank <= current.ssl_mode.security_rank:
            return None
        updated = CdnSite.model_validate({**current.model_dump(), "ssl_mode": target})
        with self.uow.transaction():
            saved = self.sites.replace_site(updated)
            self.events.record(
                domain_event(
                    operator,
                    "ssl.automatic.upgraded",
                    "site",
                    site_name,
                    {"from": current.ssl_mode.value, "to": target.value},
                )
            )
        return saved
