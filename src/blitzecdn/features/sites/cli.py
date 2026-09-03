"""`site` — the virtual hosts, and every setting that decides how one is served.

This group used to hold two read-only commands, because a site was derived from
a proxied DNS record and everything that changed one was a `record` command.
Sites are canonical now, so the ten policy commands moved here from `dns` and
the create/delete pair that never existed came with them.

What is still not here is `server_names`: the hostnames a site answers on are
the records routed to it, so they are added and removed with
'blitzecdn record route' and 'blitzecdn record unroute'.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.features.compression.policy import CompressionMode
from blitzecdn.features.security.policy import SiteFirewall
from blitzecdn.features.sites.domain import CdnSite, SitePatch
from blitzecdn.features.sites.policy import CacheQueryStringMode, SiteVisitorHeaders
from blitzecdn.features.tls.policy import MinimumTlsVersion, SslAutomaticMode, SslMode

site_app = typer.Typer(
    no_args_is_help=True,
    help="Manage CDN virtual hosts and the policy each one is served with.",
)


def _update(name: str, patch: SitePatch) -> CdnSite:
    return common.control_plane().site_editor.update_site(name, patch, "cli")


def _applied(site: CdnSite, message: str) -> str:
    """A confirmation that says whether anything is actually serving yet."""
    if site.server_names:
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} No hostname routes to {site.name!r} yet, so nothing is "
        "served — use 'blitzecdn record route' to point one here."
    )


# -- The site itself -----------------------------------------------------


@site_app.command("create")
def site_create(
    name: Annotated[
        str, typer.Argument(help="Internal site name, e.g. www-example-com.")
    ],
    origin: Annotated[
        str, typer.Option("--origin", help="Host or address the edge fetches from.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a virtual host.

    It serves nothing until a DNS record routes a hostname to it. Everything
    else — TLS, caching, compression, firewall — has a default and its own
    command; the site is usable as soon as it exists.
    """
    site = CdnSite(name=name, origin_host=origin)
    created = common.control_plane().site_editor.create_site(site, "cli")
    common.emit(created, json_output=json_output)
    if not json_output:
        typer.echo(
            f"Created site {created.name!r} fetching from {created.origin_host}. "
            f"Route a hostname to it: blitzecdn record route <zone> <name> "
            f"--site {created.name}"
        )


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the virtual hosts.

    This is desired state, not what the fleet is running: a site edited but not
    deployed is already listed here. Use 'drift' for the fleet's view.
    """
    common.emit(common.control_plane().sites.list_sites(), json_output=json_output)


@site_app.command("show")
def site_show(
    name: Annotated[str, typer.Argument(help="Site name, e.g. www-example-com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the fully resolved policy for one site.

    Where the defaults a create never mentioned become visible — it is the same
    document the edges are handed.
    """
    common.emit(common.control_plane().sites.get_site(name), json_output=json_output)


@site_app.command("origin")
def site_origin(
    name: Annotated[str, typer.Argument()],
    origin: Annotated[
        str, typer.Option("--origin", help="Host or address the edge fetches from.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Change where the edge fetches this site's content from."""
    site = _update(name, SitePatch(origin_host=origin))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(_applied(site, f"{site.name} now fetches from {site.origin_host}."))


@site_app.command("enable")
def site_enable(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Serve this site, or withdraw it.")
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Serve or withdraw a site without touching its records.

    A disabled site keeps its name, its hostnames and every setting; it simply
    stops being converged. Deleting the records instead would take the
    hostnames back out of DNS, which is a different decision.
    """
    site = _update(name, SitePatch(enabled=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(site, f"{site.name} is now {'enabled' if on else 'disabled'}.")
        )


@site_app.command("remove")
def site_remove(
    name: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Delete a site that no hostname routes to.

    Refused while records still point here, and the refusal names them: the
    order is yours to choose. To stop serving a site but keep it, use
    'site enable --off'.
    """
    if not yes and not typer.confirm(f"Delete site {name!r}?"):
        raise typer.Abort()
    common.control_plane().site_editor.delete_site(name, "cli")
    typer.echo(f"Deleted {name}")


# -- TLS -----------------------------------------------------------------


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


@site_app.command("http3")
def site_http3(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off", help="Offer HTTP/3 over QUIC on UDP/443, or withdraw it."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable visitor HTTP/3 for one TLS-enabled site.

    HTTP/2 and HTTP/1.1 remain available over TCP. This setting never changes
    the protocol used from the edge to the origin.
    """
    site = _update(name, SitePatch(http3_enabled=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"HTTP/3 is now {'enabled' if on else 'disabled'} for {site.name}.",
            )
        )


@site_app.command("always-use-https")
def site_always_use_https(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Redirect all visitor HTTP requests to HTTPS, or serve both schemes.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable the HTTP-to-HTTPS redirect for one site.

    The setting takes effect only while the SSL mode serves HTTPS. Disabling it
    leaves HTTPS available and serves HTTP requests through to the origin.
    """
    site = _update(name, SitePatch(always_use_https=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"Always Use HTTPS is now {'enabled' if on else 'disabled'} "
                f"for {site.name}.",
            )
        )


# -- Cache, compression, security ----------------------------------------


@site_app.command("cache-query-string")
def site_cache_query_string(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CacheQueryStringMode,
        typer.Option(
            "--mode", help="Include query strings in cache keys, or ignore them."
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether query strings distinguish cached responses."""
    site = _update(name, SitePatch(cache_query_string_mode=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"{site.name} cache query-string mode is now "
                f"{site.cache_query_string_mode.value!r}.",
            )
        )


@site_app.command("under-attack")
def site_under_attack(
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Challenge unverified browser traffic at the edge, or disable it.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable emergency edge challenge/mitigation mode.

    Enabling this policy also requires the fleet's Nginx under-attack
    capability and a signing secret. A deploy fails rather than ignoring a
    site whose edge cannot enforce it.
    """
    site = _update(name, SitePatch(under_attack_mode=on))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"Under Attack Mode is now {'enabled' if on else 'disabled'} "
                f"for {site.name}.",
            )
        )


@site_app.command("compression")
def site_compression(
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CompressionMode,
        typer.Option(
            "--mode",
            help="Compress at the edge with Brotli and gzip, gzip only, or not at all.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose which encodings the edge produces for this site.

    'brotli' offers Brotli to clients that accept it and gzip to the rest, and
    falls back to gzip on an edge without the Brotli module. 'off' stops the
    edge compressing; a response the origin already compressed is still passed
    through, because nginx never re-encodes an encoded body.
    """
    site = _update(name, SitePatch(compression=mode))
    common.emit(site, json_output=json_output)
    if not json_output:
        typer.echo(
            _applied(
                site,
                f"{site.name} edge compression is now {site.compression.value!r}.",
            )
        )


@site_app.command("visitor-headers")
def site_visitor_headers(
    name: Annotated[str, typer.Argument()],
    connecting_ip: Annotated[
        bool | None,
        typer.Option(
            "--connecting-ip/--no-connecting-ip",
            help="Send BZ-Connecting-IP, the visitor address the edge saw.",
        ),
    ] = None,
    ip_country: Annotated[
        bool | None,
        typer.Option(
            "--ip-country/--no-ip-country",
            help="Send BZ-IPCountry, the visitor's ISO 3166-1 alpha-2 code. "
            "Needs GeoIP on the edge.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose what the edge tells the origin about the visitor.

    BZ-Connecting-IP carries the visitor address, IPv4 or IPv6, as nginx saw it
    on the connection; it is on by default, because an origin behind the CDN
    sees an edge address on every connection. BZ-IPCountry carries the country
    the GeoIP2 database resolves that address to, and is off by default.

    Both are written by BlitzeCDN and overwrite whatever the visitor sent under
    the same name; a header turned off here is cleared rather than forwarded,
    so the BZ- namespace never carries client input. Trust them at the origin
    only where the origin accepts traffic from BlitzeCDN edges alone.

    BZ-IPCountry needs the 'geoip' capability on this controller — the
    blitzecdn-geoip distribution — and blitzecdn_geoip_enabled with a
    country database on the edge. A deploy refuses rather than send an origin a
    header that is silently absent.

    Options you do not name are left as they are.
    """
    control = common.control_plane()
    supplied = {"connecting_ip": connecting_ip, "ip_country": ip_country}
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter(
            "give at least one of --connecting-ip/--no-connecting-ip or "
            "--ip-country/--no-ip-country"
        )
    current = control.sites.get_site(name).visitor_headers
    headers = SiteVisitorHeaders.model_validate(current.model_dump() | named)
    site = control.site_editor.update_site(
        name, SitePatch(visitor_headers=headers), "cli"
    )
    common.emit(site, json_output=json_output)
    if not json_output:
        sent = [
            header
            for header, on in (
                ("BZ-Connecting-IP", site.visitor_headers.connecting_ip),
                ("BZ-IPCountry", site.visitor_headers.ip_country),
            )
            if on
        ]
        typer.echo(
            _applied(
                site,
                f"{site.name} now sends {', '.join(sent)} to the origin."
                if sent
                else f"{site.name} now sends no BZ-* visitor headers to the origin.",
            )
        )


@site_app.command("firewall")
def site_firewall(
    name: Annotated[str, typer.Argument()],
    allow_source: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-source",
            help="CIDR exempted from --deny-source. Repeatable. Replaces the list.",
        ),
    ] = None,
    deny_source: Annotated[
        list[str] | None,
        typer.Option(
            "--deny-source", help="CIDR answered with 403. Repeatable. Replaces."
        ),
    ] = None,
    allow_country: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-country",
            help="ISO 3166-1 alpha-2; every other country gets a 403. Needs GeoIP.",
        ),
    ] = None,
    deny_country: Annotated[
        list[str] | None,
        typer.Option(
            "--deny-country",
            help="ISO 3166-1 alpha-2 answered with 403. Needs GeoIP on the edge.",
        ),
    ] = None,
    deny_method: Annotated[
        list[str] | None,
        typer.Option("--deny-method", help="HTTP method answered with 405."),
    ] = None,
    deny_path: Annotated[
        list[str] | None,
        typer.Option("--deny-path", help="URI prefix answered with 403."),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove every rule and serve everyone.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Filter requests to one site at the edge.

    The posture stays open: rules subtract from a site that otherwise serves
    everyone, and --allow-source is an exemption from --deny-source rather than
    a whitelist. To close a site, deny everything and list the exceptions:

        blitzecdn site firewall www-example-com \\
            --deny-source 0.0.0.0/0 --deny-source ::/0 \\
            --allow-source 203.0.113.0/24

    Each option replaces its own list; the lists you do not name are kept. That
    differs from the equivalent PATCH on the HTTP API, which replaces the whole
    block. Country rules need the 'geoip' capability on this controller — the
    blitzecdn-geoip distribution — and blitzecdn_geoip_enabled with a
    country database on the edge. A deploy will refuse rather than silently
    serve the traffic they were meant to block.
    """
    control = common.control_plane()
    supplied = {
        "allow_sources": allow_source,
        "deny_sources": deny_source,
        "allowed_countries": allow_country,
        "denied_countries": deny_country,
        "denied_methods": deny_method,
        "denied_paths": deny_path,
    }
    named = {field: value for field, value in supplied.items() if value is not None}
    if clear and named:
        raise typer.BadParameter("--clear cannot be combined with a rule option")
    if not clear and not named:
        raise typer.BadParameter(
            "give at least one rule option, or --clear to remove every rule"
        )
    if clear:
        firewall = SiteFirewall()
    else:
        # Merged as a plain mapping and revalidated, rather than model_copy'd:
        # model_copy would install the raw lists without running a validator,
        # and these end up interpolated into an nginx directive.
        current = control.sites.get_site(name).firewall
        firewall = SiteFirewall.model_validate(current.model_dump() | named)
    site = control.site_editor.update_site(name, SitePatch(firewall=firewall), "cli")
    common.emit(site, json_output=json_output)
    if not json_output:
        rules = sum(len(getattr(site.firewall, f)) for f in SiteFirewall.model_fields)
        typer.echo(
            _applied(
                site,
                f"{site.name} now carries {rules} firewall rule(s)."
                if rules
                else f"{site.name} no longer filters any requests.",
            )
        )


__all__ = ["site_app"]
