"""Register DNS zones, records, APIs, and commands."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.dns import cli
from blitzecdn.capabilities.dns.api import routes
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="dns",
        version=__version__,
        api_version=1,
        required=True,
        summary="DNS zones, records, and site derivation.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="domain", app=cli.domain_app),
        CliCommandGroup(name="record", app=cli.record_app),
        CliCommandGroup(name="dns", app=cli.dns_app),
    )
