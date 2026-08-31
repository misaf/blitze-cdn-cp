"""Register the site-serving composition and the document the edges converge on.

One contribution, and it is the important one: the flat site document every
edge role reads. The QUIC listener state used to be derived here too and is now
``http``'s, which is the ownership rule applied to the one case where it made a
difference — a fleet fact about a protocol was being computed by the feature
that merely composes that protocol's switch.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.plugins import (
    PluginMetadata,
    SiteStateContribution,
    hookimpl,
)
from blitzecdn.features.sites.domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="sites",
        version=__version__,
        required=True,
        summary="The virtual host: every capability's site policy on one model.",
    )


@hookimpl
def blitzecdn_site_desired_state(site: CdnSite) -> SiteStateContribution:
    """Project the stable flat site contract consumed by the edge roles."""
    return SiteStateContribution(plugin="sites", variables=site_to_ansible(site))
