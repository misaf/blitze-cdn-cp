"""What the DNS feature renders: the flat site contract the edge roles read."""

from __future__ import annotations

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.plugins import FeatureRendering, SiteStateContribution


def site_contract(site: CdnSite) -> SiteStateContribution:
    """Project the stable flat site contract consumed by the edge roles."""
    return SiteStateContribution(plugin="dns", variables=site_to_ansible(site))


RENDERING = FeatureRendering(plugin="dns", site_state=site_contract)
