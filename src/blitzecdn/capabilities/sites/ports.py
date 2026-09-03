"""What this capability needs from persistence, and what it publishes outward.

Two audiences, deliberately different. ``SiteStore`` is what ``SiteService``
calls: the whole of the site table, because this capability owns it. ``SiteReader``
is what an installed distribution is handed as ``platform.sites``: it can answer
which hostnames the fleet serves without being able to write a site or reach
SQLite.

Neither of them writes ``server_names``. That column is the one part of a site
this capability does not own — `dns` maintains it from the records routed here,
through its own ``SiteHostnames`` port — so the write side of it is absent from
both protocols rather than present and documented as off-limits.
"""

from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.sites.domain import CdnSite


class SiteReader(Protocol):
    """The virtual hosts, read-only."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


class SiteStore(SiteReader, Protocol):
    """The site table, as its owning service uses it."""

    def create_site(self, site: CdnSite) -> CdnSite: ...

    def replace_site(self, site: CdnSite) -> CdnSite: ...

    def delete_site(self, name: str) -> None: ...

    def replace_all_sites(self, sites: list[CdnSite]) -> None: ...


__all__ = ["SiteReader", "SiteStore"]
