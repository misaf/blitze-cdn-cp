"""`geoip` — download the fleet's GeoLite2 database on the controller now."""

from __future__ import annotations

import typer
from dishka import FromDishka

from blitzecdn.capabilities.security.geoip.service import GeoIpService
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.delivery.cli import common

geoip_app = typer.Typer(
    no_args_is_help=True,
    help="Manage the GeoIP database the controller ships to every edge.",
)


@geoip_app.command("update")
@common.inject
def geoip_update(
    service: FromDishka[GeoIpService], json_output: common.JsonOutput = False
) -> None:
    """Download GeoLite2 from MaxMind once, for the whole fleet.

    The scheduler does this on its own; run it to provision the first database
    without waiting a refresh interval. A changed database reaches the edges
    with the next deployment.
    """
    if not service.configured:
        raise ConfigurationError(
            "BLITZE_MAXMIND_ACCOUNT_ID and BLITZE_MAXMIND_LICENSE_KEY are not "
            "set on this controller, so there is nothing to download with."
        )
    result = service.refresh("cli")
    note = (
        "The database changed; run `blitzecdn deploy` to ship it to the edges."
        if result.changed
        else "The database is already current."
    )
    common.emit(
        {
            "changed": result.changed,
            "sha256": result.sha256,
            "removed": result.removed,
        },
        json_output=json_output,
        note=note,
    )
