"""How the sites capability is built.

Named and shaped like the ``composition.py`` every optional distribution
already carries, so that "how is this capability assembled" has one answer
whichever side of the packaging boundary a capability is on.

The difference between the two kinds is the argument list, and it is the whole
of the difference. An optional distribution is handed ``platform`` and builds
itself out of what core publishes there — settings, the read side of the site
model, the event recorder, the fleet. A built-in is handed the same platform
*and* the persistence slice it declared a port for, because a built-in is what
the platform is made of: ``SiteStore`` is the write side of the site model, and
publishing it on ``ControlPlane`` so that this function could read it there
would put it one attribute away from every entry layer.

So the rule is readable from the signature. Anything a package could have is
taken from ``platform``; anything only a built-in may have is an explicit
keyword argument, and there are never many.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.sites.ports import SiteStore
from blitzecdn.capabilities.sites.service import SiteService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.bootstrap import ControlPlane

__all__ = ["build_site_service"]


def build_site_service(platform: ControlPlane, *, sites: SiteStore) -> SiteService:
    """Wire the editor that owns everything about how a site is served.

    ``sites`` is the write side and arrives separately for that reason:
    ``platform.sites`` is the same store seen through ``SiteReader``, which is
    what a package that only has to answer "which hostnames does the fleet
    serve" receives.
    """
    return SiteService(
        sites=sites,
        events=platform.events,
        uow=platform.transactions,
    )
