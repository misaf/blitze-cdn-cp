"""`domain`, `record`, `dns` and `site` — the zone editor's command groups.

They share a module because they edit one thing: records are the source of
truth, and sites are what proxying a record derives.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.domain.dns import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.domain.sites import (
    CacheQueryStringMode,
    CompressionMode,
    MinimumTlsVersion,
    SiteFirewall,
    SiteVisitorHeaders,
    SslAutomaticMode,
    SslMode,
)

site_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect CDN virtual hosts (derived from proxied DNS records).",
)
domain_app = typer.Typer(no_args_is_help=True, help="Manage DNS zones.")
record_app = typer.Typer(
    no_args_is_help=True,
    help="Manage DNS records, and the CDN policy each proxied record carries.",
)
dns_app = typer.Typer(no_args_is_help=True, help="Export DNS state.")


# -- Sites ---------------------------------------------------------------


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the virtual hosts the edges will serve.

    Derived, not created: only proxied records appear. A record you proxied but
    have not deployed is already listed here — this is desired state, not what
    the fleet is running. Use 'drift' for that.
    """
    common.emit(common.control_plane().dns.list_sites(), json_output=json_output)


@site_app.command("show")
def site_show(
    name: Annotated[str, typer.Argument(help="Site name, e.g. cdn-example-com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the fully resolved policy for one site.

    Sites are derived from proxied DNS records rather than created, so this is
    where the defaults a record never mentions become visible — it is the same
    document the edges are handed.
    """
    common.emit(common.control_plane().dns.get_site(name), json_output=json_output)


# -- Domains -------------------------------------------------------------


@domain_app.command("add")
def domain_add(
    name: Annotated[str, typer.Argument(help="Zone to serve, e.g. example.com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a DNS zone delegated to BlitzeCDN."""
    common.emit(
        common.control_plane().dns.create_domain(Domain(name=name), "cli"),
        json_output=json_output,
    )


@domain_app.command("list")
def domain_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the DNS zones delegated to BlitzeCDN."""
    common.emit(common.control_plane().dns.list_domains(), json_output=json_output)


@domain_app.command("remove")
def domain_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a zone and every record in it."""
    if not yes and not typer.confirm(f"Delete {name!r} and all of its records?"):
        raise typer.Abort()
    common.control_plane().dns.delete_domain(name, "cli")
    typer.echo(f"Deleted {name}")


# -- Records -------------------------------------------------------------


@record_app.command("add")
def record_add(
    domain: Annotated[str, typer.Argument(help="Zone the record belongs to.")],
    name: Annotated[str, typer.Argument(help="Subdomain label, '@', or '*'.")],
    value: Annotated[str, typer.Option("--value", help="IP address to point at.")],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    ttl: Annotated[int, typer.Option("--ttl")] = 300,
    proxied: Annotated[
        bool,
        typer.Option(
            "--proxied/--no-proxied",
            help="Serve through the CDN edge, or resolve straight to --value.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add a DNS record. Proxied records become an edge virtual host."""
    record = DnsRecord(
        domain=domain, name=name, type=type_, value=value, ttl=ttl, proxied=proxied
    )
    common.emit(
        common.control_plane().dns.create_record(record, "cli"),
        json_output=json_output,
    )


@record_app.command("list")
def record_list(
    domain: Annotated[str | None, typer.Argument()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List records, with the CDN policy each one carries.

    Shows every zone unless you name one. Proxied records carry the origin,
    cache, TLS and firewall settings that become their edge site; unproxied
    records carry them too, kept but unused, so toggling the proxy back on
    does not lose them.
    """
    common.emit(
        common.control_plane().dns.list_records(domain), json_output=json_output
    )


@record_app.command("proxy")
def record_proxy(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Route through the CDN, or bypass it.")
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Turn the CDN on or off for one record.

    Takes effect on the edge at the next deploy. It only reaches clients once
    DNS answers accordingly, which the DNS system owns.
    """
    record = common.control_plane().dns.update_record(
        domain, name, type_, RecordPatch(proxied=on), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} is now "
            f"{'proxied through the CDN' if on else 'bypassing the CDN'}. "
            "Run 'blitzecdn deploy' to apply, and make sure DNS points at "
            f"{'an edge' if on else record.value}."
        )


@record_app.command("ssl")
def record_ssl(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        SslMode,
        typer.Option(
            "--mode",
            help="Off, Flexible, Full, or Full (strict) edge/origin TLS policy.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set visitor and origin encryption for one hostname.

    Flexible encrypts visitors but uses HTTP to the origin. Full uses HTTPS to
    the origin without verifying its certificate. Full (strict) verifies the
    origin certificate and hostname. Every mode except Off requires an active
    edge certificate.
    """
    record = common.control_plane().dns.update_record(
        domain, name, type_, RecordPatch(ssl_mode=mode), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} now uses SSL mode {record.ssl_mode.value!r}. "
            "Run 'blitzecdn deploy' to apply."
        )


@record_app.command("ssl-automatic")
def record_ssl_automatic(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        SslAutomaticMode,
        typer.Option(
            "--mode",
            help="Auto upgrades after origin scans; Custom preserves ssl_mode.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enroll a hostname in Automatic SSL/TLS or opt it into Custom mode."""
    record = common.control_plane().dns.update_record(
        domain,
        name,
        type_,
        RecordPatch(ssl_automatic_mode=mode),
        "cli",
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} now uses SSL automatic mode "
            f"{record.ssl_automatic_mode.value!r}."
        )


@record_app.command("minimum-tls")
def record_minimum_tls(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    version: Annotated[
        MinimumTlsVersion,
        typer.Option(
            "--version",
            help="Oldest visitor TLS version accepted at the edge: 1.2 or 1.3.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set the minimum visitor TLS version for one hostname."""
    record = common.control_plane().dns.update_record(
        domain,
        name,
        type_,
        RecordPatch(minimum_tls_version=version),
        "cli",
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} now requires TLS {record.minimum_tls_version.value} "
            "or newer. Run 'blitzecdn deploy' to apply."
        )


@record_app.command("http3")
def record_http3(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Offer HTTP/3 over QUIC on UDP/443, or withdraw it.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable visitor HTTP/3 for one TLS-enabled hostname.

    HTTP/2 and HTTP/1.1 remain available over TCP. This setting never changes
    the protocol used from the edge to the origin.
    """
    record = common.control_plane().dns.update_record(
        domain, name, type_, RecordPatch(http3_enabled=on), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"HTTP/3 is now {'enabled' if on else 'disabled'} for "
            f"{record.fqdn}. Run 'blitzecdn deploy' to apply."
        )


@record_app.command("always-use-https")
def record_always_use_https(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Redirect all visitor HTTP requests to HTTPS, or serve both schemes.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable the HTTP-to-HTTPS redirect for one hostname.

    The setting takes effect only while the SSL mode serves HTTPS. Disabling it
    leaves HTTPS available and serves HTTP requests through to the origin.
    """
    record = common.control_plane().dns.update_record(
        domain, name, type_, RecordPatch(always_use_https=on), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"Always Use HTTPS is now {'enabled' if on else 'disabled'} for "
            f"{record.fqdn}. Run 'blitzecdn deploy' to apply."
        )


@record_app.command("cache-query-string")
def record_cache_query_string(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CacheQueryStringMode,
        typer.Option(
            "--mode",
            help="Include query strings in cache keys, or ignore them.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose whether query strings distinguish cached responses."""
    record = common.control_plane().dns.update_record(
        domain,
        name,
        type_,
        RecordPatch(cache_query_string_mode=mode),
        "cli",
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} cache query-string mode is now "
            f"{record.cache_query_string_mode.value!r}. "
            "Run 'blitzecdn deploy' to apply."
        )


@record_app.command("under-attack")
def record_under_attack(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    on: Annotated[
        bool,
        typer.Option(
            "--on/--off",
            help="Challenge unverified browser traffic at the edge, or disable it.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enable or disable emergency edge challenge/mitigation mode.

    Enabling this policy also requires the fleet's Nginx under-attack
    capability and a signing secret. A deploy fails rather than ignoring a
    site whose edge cannot enforce it.
    """
    record = common.control_plane().dns.update_record(
        domain, name, type_, RecordPatch(under_attack_mode=on), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"Under Attack Mode is now {'enabled' if on else 'disabled'} for "
            f"{record.fqdn}. Run 'blitzecdn deploy' to apply."
        )


@record_app.command("compression")
def record_compression(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    mode: Annotated[
        CompressionMode,
        typer.Option(
            "--mode",
            help="Compress at the edge with Brotli and gzip, gzip only, or not at all.",
        ),
    ],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Choose which encodings the edge produces for this hostname.

    'brotli' offers Brotli to clients that accept it and gzip to the rest, and
    falls back to gzip on an edge without the Brotli module. 'off' stops the
    edge compressing; a response the origin already compressed is still passed
    through, because nginx never re-encodes an encoded body.
    """
    record = common.control_plane().dns.update_record(
        domain,
        name,
        type_,
        RecordPatch(compression=mode),
        "cli",
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} edge compression is now "
            f"{record.compression.value!r}. "
            "Run 'blitzecdn deploy' to apply."
        )


@record_app.command("visitor-headers")
def record_visitor_headers(
    domain: Annotated[str, typer.Argument()],
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
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
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

    BZ-IPCountry needs blitzecdn_nginx_geoip_enabled and a MaxMind database on
    the edge. A deploy refuses rather than send an origin a header that is
    silently absent.

    Options you do not name are left as they are. Nothing changes on the edge
    until the next 'blitzecdn deploy'.
    """
    control = common.control_plane()
    supplied = {"connecting_ip": connecting_ip, "ip_country": ip_country}
    named = {field: value for field, value in supplied.items() if value is not None}
    if not named:
        raise typer.BadParameter(
            "give at least one of --connecting-ip/--no-connecting-ip or "
            "--ip-country/--no-ip-country"
        )
    current = control.dns.get_record(domain, name, type_).visitor_headers
    headers = SiteVisitorHeaders.model_validate(current.model_dump() | named)
    record = control.dns.update_record(
        domain, name, type_, RecordPatch(visitor_headers=headers), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        sent = [
            header
            for header, on in (
                ("BZ-Connecting-IP", record.visitor_headers.connecting_ip),
                ("BZ-IPCountry", record.visitor_headers.ip_country),
            )
            if on
        ]
        typer.echo(
            f"{record.fqdn} now sends {', '.join(sent)} to the origin. "
            "Run 'blitzecdn deploy' to apply."
            if sent
            else f"{record.fqdn} now sends no BZ-* visitor headers to the "
            "origin. Run 'blitzecdn deploy' to apply."
        )


@record_app.command("firewall")
def record_firewall(
    domain: Annotated[str, typer.Argument()],
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
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Filter requests to one hostname at the edge.

    The posture stays open: rules subtract from a site that otherwise serves
    everyone, and --allow-source is an exemption from --deny-source rather than
    a whitelist. To close a hostname, deny everything and list the exceptions:

        blitzecdn record firewall example.com www \\
            --deny-source 0.0.0.0/0 --deny-source ::/0 \\
            --allow-source 203.0.113.0/24

    Each option replaces its own list; the lists you do not name are kept. That
    differs from the equivalent PATCH on the HTTP API, which replaces the whole
    block. Country rules need blitzecdn_nginx_geoip_enabled and a MaxMind
    database on the edge, and a deploy will refuse rather than silently serve
    the traffic they were meant to block.

    Nothing changes on the edge until the next 'blitzecdn deploy'.
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
        current = control.dns.get_record(domain, name, type_).firewall
        firewall = SiteFirewall.model_validate(current.model_dump() | named)
    record = control.dns.update_record(
        domain, name, type_, RecordPatch(firewall=firewall), "cli"
    )
    common.emit(record, json_output=json_output)
    if not json_output:
        rules = sum(len(getattr(record.firewall, f)) for f in SiteFirewall.model_fields)
        typer.echo(
            f"{record.fqdn} now carries {rules} firewall rule(s). "
            "Run 'blitzecdn deploy' to apply."
            if rules
            else f"{record.fqdn} no longer filters any requests. "
            "Run 'blitzecdn deploy' to apply."
        )


@record_app.command("remove")
def record_remove(
    domain: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    type_: Annotated[RecordType, typer.Option("--type")] = RecordType.A,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Delete one record, and the edge site derived from it.

    Deleting a proxied record withdraws its virtual host at the next deploy,
    along with any certificate BlitzeCDN manages for it. To take a hostname
    off the edge but keep its settings, use 'record proxy --off' instead.
    """
    label = f"{name}.{domain}" if name != "@" else domain
    if not yes and not typer.confirm(f"Delete {type_.value} record for {label!r}?"):
        raise typer.Abort()
    common.control_plane().dns.delete_record(domain, name, type_, "cli")
    typer.echo(f"Deleted {label}")


# -- Export --------------------------------------------------------------


@dns_app.command("export")
def dns_export(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Emit every record for the system that publishes DNS.

    Proxied records carry no address: they must resolve to an edge, and edge
    addressing is owned by the DNS system rather than the control plane.
    """
    common.emit(common.control_plane().dns.dns_export(), json_output=json_output)
