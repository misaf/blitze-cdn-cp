"""Register the site-serving composition and the document the edges converge on.

The important contribution is still the flat site document every edge role
reads; beside it are the `site` commands and routes, which live here because
this is where someone looks for them. They stopped being read-only when a site
stopped being a projection of a DNS record: creating, editing and deleting one
is this capability's own operation now, and `dns` contributes only the routing.

The QUIC listener state used to be derived here too. It moved to ``http``
first — the ownership rule applied to the one case where it made a difference,
a fleet fact about a protocol being computed by the capability that merely
composes that protocol's switch — and then out of this distribution
altogether, into ``blitzecdn-http3``. What stays here is the switch's *value*
on each site document, which is site policy like any other.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.sites import cli
from blitzecdn.capabilities.sites.api import routes
from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    SiteStateContribution,
    hookimpl,
)


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


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="site", app=cli.site_app),)
