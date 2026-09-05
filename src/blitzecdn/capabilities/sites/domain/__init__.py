"""What a site is, and what a request to change one is.

Two modules, one per value type, named for the thing they hold rather than for
the layer they sit in — the shape `edges/domain/` and `deployments/domain/`
already had. `site.py` is the virtual host: `SitePolicy`, the composition of
every capability's contract, and `CdnSite`, the identity and the cross-capability
rules on top of it. `patch.py` is one partial update to a site, together with
the import-time check that it can still express every setting `SitePolicy`
carries.

This was one 469-line `domain.py`, the largest domain module in the workspace
and the only one whose name said "the domain layer of this slice" rather than
what it held. The package is the capability's public face either way, so
`sites.domain` still means what it did when it was a file.
"""

from blitzecdn.capabilities.sites.domain.patch import SitePatch
from blitzecdn.capabilities.sites.domain.site import CdnSite, SitePolicy

__all__ = ["CdnSite", "SitePatch", "SitePolicy"]
