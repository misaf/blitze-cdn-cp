"""The `tls` contract's switches: encryption mode and minimum visitor version."""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.editing import report, update
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.tls.contract import (
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.entrypoints.cli import common

#: Joined to `blitzecdn domain`, which dns founds: these edit the zone's
#: tls settings, so they ship with the capability that owns them.
domain_commands = typer.Typer()


@domain_commands.command("ssl")
def domain_ssl(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        SslMode,
        typer.Option(
            "--mode",
            help="Off, Flexible, Full, or Full (strict) edge/origin TLS policy.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Set visitor and origin encryption for one zone.

    Flexible encrypts visitors but uses HTTP to the origin. Full uses HTTPS to
    the origin without verifying its certificate. Full (strict) verifies the
    origin certificate and hostname. Every mode except Off requires an active
    edge certificate.
    """
    zone = update(name, DomainPatch(ssl_mode=mode))
    report(
        zone,
        f"{zone.name} now uses SSL mode {zone.policy.tls.ssl_mode.value!r}.",
        json_output=json_output,
    )


@domain_commands.command("ssl-automatic")
def domain_ssl_automatic(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    mode: Annotated[
        SslAutomaticMode,
        typer.Option(
            "--mode",
            help="Auto upgrades after origin scans; Custom preserves ssl_mode.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Enroll a zone in Automatic SSL/TLS or opt it into Custom mode."""
    zone = update(name, DomainPatch(ssl_automatic_mode=mode))
    common.emit(
        zone,
        json_output=json_output,
        note=(
            f"{zone.name} now uses SSL automatic mode "
            f"{zone.policy.tls.ssl_automatic_mode.value!r}."
        ),
    )


@domain_commands.command("minimum-tls")
def domain_minimum_tls(
    name: Annotated[str, typer.Argument(help="Zone, e.g. example.com.")],
    version: Annotated[
        MinimumTlsVersion,
        typer.Option(
            "--version",
            help="Oldest visitor TLS version accepted at the edge: 1.2 or 1.3.",
        ),
    ],
    json_output: common.JsonOutput = False,
) -> None:
    """Set the minimum visitor TLS version for one zone."""
    zone = update(name, DomainPatch(minimum_tls_version=version))
    report(
        zone,
        f"{zone.name} now requires TLS "
        f"{zone.policy.tls.minimum_tls_version.value} or newer.",
        json_output=json_output,
    )
