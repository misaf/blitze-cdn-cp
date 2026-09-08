"""The `tls` contract\'s switches: encryption mode and minimum visitor version.

Edits fields declared by :mod:`blitzecdn.capabilities.tls.policy`. Issuing and
renewing the certificate those modes require is not here and not in this
distribution — that is `blitzecdn-certificates`, which contributes its own
commands.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.dns.cli.app import _report, _update, domain_app
from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.tls.policy import (
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.cli import common


@domain_app.command("ssl")
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
    zone = _update(name, DomainPatch(ssl_mode=mode))
    _report(
        zone,
        f"{zone.name} now uses SSL mode {zone.ssl_mode.value!r}.",
        json_output=json_output,
    )


@domain_app.command("ssl-automatic")
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
    zone = _update(name, DomainPatch(ssl_automatic_mode=mode))
    common.emit(
        zone,
        json_output=json_output,
        note=(
            f"{zone.name} now uses SSL automatic mode "
            f"{zone.ssl_automatic_mode.value!r}."
        ),
    )


@domain_app.command("minimum-tls")
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
    zone = _update(name, DomainPatch(minimum_tls_version=version))
    _report(
        zone,
        f"{zone.name} now requires TLS {zone.minimum_tls_version.value} or newer.",
        json_output=json_output,
    )
