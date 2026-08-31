"""A well-behaved external plugin: the documented example, kept executable."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter
from typer import Typer

from blitzecdn.core.plugins import (
    CliCommandGroup,
    FleetStateContribution,
    HealthCheck,
    PluginMetadata,
    ScheduledJob,
    SiteStateContribution,
    ValidationIssue,
    hookimpl,
)

router = APIRouter()
commands = Typer()


@router.get("/v1/waf/status")
def waf_status() -> dict[str, str]:
    return {"status": "enforcing"}


@commands.command("show")
def waf_show() -> None:
    print("enforcing")


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(name="waf", version="1.2.0", summary="A pretend WAF.")


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    return (CliCommandGroup(name="waf", app=commands),)


@hookimpl
def blitzecdn_health_checks(platform: object) -> Sequence[HealthCheck]:
    return (HealthCheck(name="waf-rules", check=lambda: None),)


@hookimpl
def blitzecdn_scheduled_jobs(platform: object) -> Sequence[ScheduledJob]:
    return (
        ScheduledJob(
            name="waf-rule-refresh",
            interval_seconds=900,
            run=lambda operator: None,
            jitter_seconds=30,
        ),
    )


@hookimpl
def blitzecdn_site_desired_state(site: object) -> SiteStateContribution:
    return SiteStateContribution(
        plugin="waf", variables={"waf_enabled": True, "waf_mode": "enforce"}
    )


@hookimpl
def blitzecdn_fleet_desired_state(sites: tuple[object, ...]) -> FleetStateContribution:
    return FleetStateContribution(
        plugin="waf", variables={"blitzecdn_edge_waf_enabled": bool(sites)}
    )


@hookimpl
def blitzecdn_deployment_checks(site: object) -> Sequence[ValidationIssue]:
    return ()
