"""Register zones, their policy, their rules, their records — and what edges get.

The flat site document every edge role reads is contributed from here now. It
was `sites`' contribution when a site was a thing an operator authored; the
capability that composes a virtual host is the one that projects it, and that
is this one.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.dns import cli
from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.api import routes
from blitzecdn.capabilities.dns.domain import CdnSite
from blitzecdn.core.plugins import (
    CliCommandGroup,
    PluginMetadata,
    SiteStateContribution,
    hookimpl,
)


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="dns",
        version=__version__,
        api_version=1,
        required=True,
        summary="DNS zones, their policy, the rules on it, and their records.",
    )


@hookimpl
def blitzecdn_site_desired_state(site: CdnSite) -> SiteStateContribution:
    """Project the stable flat site contract consumed by the edge roles."""
    return SiteStateContribution(plugin="dns", variables=site_to_ansible(site))


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(plugin="dns", name="domain", app=cli.domain_app),
        CliCommandGroup(plugin="dns", name="record", app=cli.record_app),
        CliCommandGroup(plugin="dns", name="rule", app=cli.rule_app),
        CliCommandGroup(plugin="dns", name="dns", app=cli.dns_app),
    )
