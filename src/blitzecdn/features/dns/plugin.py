"""Zones, records, and the `CdnSite` projection every other feature reads.

Contributes the *base* desired-state document for a site: the site model dumped
as the edge roles expect it. Other plugins add to that document and, where they
mean to, override a key in it — see `certificates/plugin.py`, which replaces the
two TLS paths because only this controller knows where the material actually is.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    SiteStateContribution,
    hookimpl,
)
from blitzecdn.features.dns import cli
from blitzecdn.features.dns.api import v1, v1_sites, v2, v2_sites
from blitzecdn.features.dns.site_domain import CdnSite


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="dns",
        version=__version__,
        required=True,
        summary="Zones, records, and the virtual hosts derived from them.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1_sites.router, v1.router, v2_sites.router, v2.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="site", app=cli.site_app),
        CliCommandGroup(name="domain", app=cli.domain_app),
        CliCommandGroup(name="record", app=cli.record_app),
        CliCommandGroup(name="dns", app=cli.dns_app),
    )


@hookimpl
def blitzecdn_site_desired_state(site: CdnSite) -> SiteStateContribution:
    """The site itself, as the nginx role's argument spec declares it."""
    return SiteStateContribution(plugin="dns", variables=site_to_ansible(site))
