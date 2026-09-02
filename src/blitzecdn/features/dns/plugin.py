"""Register DNS zones, records, APIs, and commands."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.core.plugins import CliCommandGroup, PluginMetadata, hookimpl
from blitzecdn.features.dns import cli
from blitzecdn.features.dns.api import v1, v2


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="dns",
        version=__version__,
        required=True,
        summary="DNS zones, records, and site derivation.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (v1.router, v2.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (
        CliCommandGroup(name="domain", app=cli.domain_app),
        CliCommandGroup(name="record", app=cli.record_app),
        CliCommandGroup(name="dns", app=cli.dns_app),
    )
