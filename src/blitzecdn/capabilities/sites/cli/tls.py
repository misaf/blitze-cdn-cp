"""The `tls` contract\'s switches: encryption mode and minimum visitor version.

Edits fields declared by :mod:`blitzecdn.capabilities.tls.policy`. Issuing and
renewing the certificate those modes require is not here and not in this
distribution — that is `blitzecdn-certificates`, which contributes its own
commands.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.capabilities.sites.cli.app import _applied, _update, site_app
from blitzecdn.capabilities.sites.domain import SitePatch
from blitzecdn.capabilities.tls.policy import (
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.cli import common


@site_app.command("ssl")
def site_ssl(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        SslMode,
        typer.Option(
            "--mode",
            help="Off, Flexible, Full, or Full (strict) edge/origin TLS policy.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set visitor and origin encryption for one site.

    Flexible encrypts visitors but uses HTTP to the origin. Full uses HTTPS to
    the origin without verifying its certificate. Full (strict) verifies the
    origin certificate and hostname. Every mode except Off requires an active
    edge certificate.
    """
    site = _update(name, SitePatch(ssl_mode=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(site, f"{site.name} now uses SSL mode {site.ssl_mode.value!r}.")
        )


@site_app.command("ssl-automatic")
def site_ssl_automatic(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        SslAutomaticMode,
        typer.Option(
            "--mode",
            help="Auto upgrades after origin scans; Custom preserves ssl_mode.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enroll a site in Automatic SSL/TLS or opt it into Custom mode."""
    site = _update(name, SitePatch(ssl_automatic_mode=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{site.name} now uses SSL automatic mode "
            f"{site.ssl_automatic_mode.value!r}."
        )


@site_app.command("minimum-tls")
def site_minimum_tls(
    name: Annotated[str, typer.Argument()],
    version: Annotated[
        MinimumTlsVersion,
        typer.Option(
            "--version",
            help="Oldest visitor TLS version accepted at the edge: 1.2 or 1.3.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set the minimum visitor TLS version for one site."""
    site = _update(name, SitePatch(minimum_tls_version=version))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"{site.name} now requires TLS "
                f"{site.minimum_tls_version.value} or newer.",
            )
        )
