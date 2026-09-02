"""What reading the site model looks like from outside `sites`.

The projection is derived by `dns` and read by everyone else, so the read side
is a contract of its own rather than a courtesy view of the store. An installed
distribution is handed `SiteReader` as `platform.sites`: it can answer which
hostnames the fleet serves without being able to write a site, reach SQLite, or
know that a zone is where the answer came from.
"""

from __future__ import annotations

from typing import Protocol

from blitzecdn.features.sites.domain import CdnSite


class SiteReader(Protocol):
    """The derived virtual hosts, read-only."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


__all__ = ["SiteReader"]
