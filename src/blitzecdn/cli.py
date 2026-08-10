from __future__ import annotations

import json
import secrets
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
import uvicorn
import yaml
from pydantic import ValidationError

from blitzecdn import EDGE_COLLECTION_VERSION, __version__
from blitzecdn.api import create_app
from blitzecdn.application import ControlPlane
from blitzecdn.config import Settings
from blitzecdn.domain.models import (
    CERTIFICATE_RENEWAL_DAYS,
    DESIRED_STATE_VERSION,
    CacheStatsReport,
    CertificateMode,
    Deployment,
    DeploymentStatus,
    DnsRecord,
    Domain,
    OriginScheme,
    PurgeEntry,
    RecordPatch,
    RecordType,
    SiteFirewall,
)
from blitzecdn.exceptions import BlitzeError
from blitzecdn.infrastructure.inventory import Inventory
from blitzecdn.infrastructure.preflight import check_resolver
from blitzecdn.logging import configure_logging


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    CONFIGURATION = 3
    CONFLICT = 4
    DEPLOYMENT_FAILED = 5
    #: `drift` found a reachable edge that no longer matches desired state, or
    #: an edge it could not reach. Distinct from DEPLOYMENT_FAILED so a
    #: scheduled check can tell "the fleet moved" from "the check itself broke".
    DRIFT_DETECTED = 6


app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="""Securely manage BlitzeCDN edge desired state.

    You do not create virtual hosts. Register a zone with 'domain add', add a
    record with 'record add --proxied', and the edge site is derived from it —
    unproxy the record and the site is gone. Per-hostname settings (origin,
    caching, TLS, firewall) live on the record.

    Nothing reaches an edge until 'deploy'. Commands change local desired state
    only, so you can stage a batch of edits and apply them in one converge.
    Preview with 'plan', apply with 'deploy', undo with 'rollback', and check
    the fleet still matches with 'drift'.
    """,
)
site_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect CDN virtual hosts (derived from proxied DNS records).",
)
edge_app = typer.Typer(no_args_is_help=True, help="Manage edge servers.")
domain_app = typer.Typer(no_args_is_help=True, help="Manage DNS zones.")
record_app = typer.Typer(
    no_args_is_help=True,
    help="Manage DNS records, and the CDN policy each proxied record carries.",
)
dns_app = typer.Typer(no_args_is_help=True, help="Export DNS state.")
origin_app = typer.Typer(
    no_args_is_help=True, help="Check the origins the edges proxy to."
)
cert_app = typer.Typer(
    no_args_is_help=True, help="Inspect and renew managed TLS certificates."
)
cache_app = typer.Typer(
    no_args_is_help=True, help="Purge cached responses from the edges."
)
app.add_typer(site_app, name="site")
app.add_typer(edge_app, name="edge")
app.add_typer(domain_app, name="domain")
app.add_typer(record_app, name="record")
app.add_typer(dns_app, name="dns")
app.add_typer(cert_app, name="cert")
app.add_typer(origin_app, name="origin")
app.add_typer(cache_app, name="cache")


def _settings() -> Settings:
    return Settings.from_environment()


def _control_plane() -> ControlPlane:
    return ControlPlane(_settings())


def _emit(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in value
        ]
    if json_output:
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
    else:
        # Every caller hands this a model, a list, or a dict, and safe_dump
        # renders all three. There is deliberately no scalar branch: a command
        # that wants to print a bare string calls typer.echo itself.
        typer.echo(yaml.safe_dump(value, sort_keys=False).rstrip())


def _version_callback(value: bool) -> None:
    """Print all three versions that have to agree, not just this package.

    A control plane, the edge collection it pins, and the desired-state schema
    it emits are separate moving parts, and the failure they produce together
    — a role refusing a schema version — names numbers an operator otherwise
    has to dig out of ``requirements.yml`` and the domain model.
    """
    if not value:
        return
    typer.echo(f"blitzecdn {__version__}")
    typer.echo(f"blitzecdn.edge {EDGE_COLLECTION_VERSION} (pinned)")
    typer.echo(f"desired-state schema {DESIRED_STATE_VERSION}")
    raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable diagnostic logging.")
    ] = False,
    log_json: Annotated[bool, typer.Option(help="Write logs as JSON.")] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the control plane, edge collection, and schema versions.",
        ),
    ] = False,
) -> None:
    configure_logging(verbose=verbose, json_output=log_json)


def _environment_file_body() -> str:
    """The scaffolded `.env`, with the optional settings named but unset.

    Commented rather than omitted: an operator who never reads the reference
    should still be able to discover that these exist, and a key that has to be
    exported into the shell before each deploy gets exported once and then
    forgotten. `.env` is written 0600 and is the intended home for both.
    """
    return (
        f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n"
        "\n"
        "# MaxMind GeoLite2, for per-hostname country filtering. Free, but the\n"
        "# download is authenticated: create an account, generate a license key,\n"
        "# and set blitzecdn_nginx_geoip_enabled in group_vars. The license key\n"
        "# is an account credential — this file is 0600 and must stay uncommitted.\n"
        "# BLITZE_MAXMIND_ACCOUNT_ID=\n"
        "# BLITZE_MAXMIND_LICENSE_KEY=\n"
    )


@app.command()
def init(
    output: Annotated[Path, typer.Option(help="Environment file to create.")] = Path(
        ".env"
    ),
) -> None:
    """Create a restrictive local environment file without overwriting one."""
    if output.exists():
        raise typer.BadParameter(f"refusing to overwrite {output}")
    output.write_text(_environment_file_body(), encoding="utf-8")
    output.chmod(0o600)
    typer.echo(f"Created {output} with mode 0600")


@app.command()
def setup() -> None:
    """Prepare local configuration and an empty edge inventory."""
    root = Path.cwd()
    environment_path = root / ".env"
    inventory_path = root / "ansible/inventory/hosts.yml"
    created: list[str] = []
    if not environment_path.exists():
        environment_path.write_text(_environment_file_body(), encoding="utf-8")
        environment_path.chmod(0o600)
        created.append(str(environment_path.relative_to(root)))
    if Inventory(inventory_path).initialize():
        created.append(str(inventory_path.relative_to(root)))
    if created:
        typer.echo(f"BlitzeCDN is ready. Created: {', '.join(created)}")
    else:
        typer.echo("BlitzeCDN is already set up; existing files were preserved.")
    typer.echo("Next: blitzecdn edge add NAME --host ADDRESS --ssh-source YOUR_CIDR")


@app.command()
def validate(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Validate configuration, desired state, inventory, and playbook syntax."""
    errors = _control_plane().validate()
    _emit({"valid": not errors, "errors": errors}, json_output=json_output)
    if errors:
        raise typer.Exit(ExitCode.CONFIGURATION)


_LIMIT_OPTION = typer.Option(
    "--limit",
    help=(
        "Restrict this run to these edges: names or globs, comma separated. "
        "Resolved against the inventory, so it can only ever narrow the fleet."
    ),
)


@app.command()
def plan(
    limit: Annotated[str | None, _LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run Ansible check mode and show the resulting deployment record."""
    result = _control_plane().deploy("cli", check=True, host_limit=limit)
    _emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@app.command()
def deploy(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Required confirmation for non-interactive execution."
        ),
    ] = False,
    limit: Annotated[str | None, _LIMIT_OPTION] = None,
    request_certificates: Annotated[
        bool,
        typer.Option(
            "--request-certificates",
            help=(
                "After the HTTP deployment, issue certificates for ready "
                "proxied sites and deploy again to install them."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate, preview, and apply desired state to the configured edges.

    With --limit this is a canary: the named edges converge and the rest keep
    serving what they have. Re-run without it to finish the rollout.
    """
    if request_certificates and limit:
        raise typer.BadParameter(
            "--request-certificates cannot be combined with --limit; HTTP-01 "
            "must be deployed to the full edge fleet"
        )
    control = _control_plane()
    if not yes:
        errors = control.validate()
        if errors:
            _emit({"valid": False, "errors": errors}, json_output=json_output)
            raise typer.Exit(ExitCode.CONFIGURATION)
        typer.echo("Configuration is valid. Previewing changes...")
        preview = control.deploy("cli", check=True, host_limit=limit)
        if preview.status is not DeploymentStatus.SUCCEEDED:
            _emit(preview, json_output=json_output)
            raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)
        if preview.stdout.strip():
            typer.echo(preview.stdout.rstrip())
        target = f"edges matching {limit!r}" if limit else "all configured edges"
        if not typer.confirm(f"Apply these changes to {target}?"):
            raise typer.Abort()
    result = control.deploy("cli", host_limit=limit)
    if result.status is not DeploymentStatus.SUCCEEDED:
        _emit(result, json_output=json_output)
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)

    issued: list[str] = []
    skipped: dict[str, str] = {}
    certificate_deployment = None
    if request_certificates:
        for site in control.repository.list_sites():
            if site.certificate_mode is not CertificateMode.DISABLED:
                continue
            report = control.certificate_preflight(site.name)
            if not report.ok:
                skipped[site.name] = report.summary()
                continue
            control.request_certificate(site.name, "cli")
            issued.append(site.name)
        if issued:
            certificate_deployment = control.deploy("cli")
            if certificate_deployment.status is not DeploymentStatus.SUCCEEDED:
                _emit(certificate_deployment, json_output=json_output)
                raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)

    if request_certificates:
        _emit(
            {
                "deployment": result.model_dump(mode="json"),
                "certificates_issued": issued,
                "certificates_skipped": skipped,
                "certificate_deployment": (
                    certificate_deployment.model_dump(mode="json")
                    if certificate_deployment is not None
                    else None
                ),
            },
            json_output=json_output,
        )
    else:
        _emit(result, json_output=json_output)
    if not json_output and limit:
        typer.echo(
            f"\nThis was a canary against {limit!r}. Every other edge is still "
            "serving its previous configuration; re-run 'blitzecdn deploy' "
            "without --limit to finish the rollout."
        )


@app.command()
def rollback(
    deployment_id: Annotated[
        str | None,
        typer.Argument(
            help="Successful deployment ID; defaults to the latest different state."
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Preview without changing canonical state.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Converge a prior successful snapshot, then make it canonical on success."""
    if (
        not check
        and not yes
        and not typer.confirm(
            "Rollback edge configuration and canonical desired state?"
        )
    ):
        raise typer.Abort()
    result = _control_plane().rollback("cli", deployment_id, check=check)
    _emit(result, json_output=json_output)
    if result.status is not DeploymentStatus.SUCCEEDED:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@app.command()
def drift(
    limit: Annotated[str | None, _LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report whether the edges still match the declared desired state.

    Changes nothing: it is a check-mode run read as a question. Exits 6 when a
    reachable edge would change, so it can be scheduled and alerted on.
    """
    report = _control_plane().check_drift("cli", host_limit=limit)
    _emit(
        {
            "deployment_id": report.deployment_id,
            "checked_at": report.checked_at.isoformat(),
            "in_sync": report.in_sync,
            "hosts": [host.model_dump(mode="json") for host in report.hosts],
        },
        json_output=json_output,
    )
    if json_output:
        pass
    elif not report.hosts:
        typer.echo(
            "\nNo edge reported a result. Check that the inventory has hosts "
            "and that the run above completed.",
            err=True,
        )
    elif report.in_sync:
        typer.echo(f"\nAll {len(report.hosts)} edges match desired state.")
    else:
        for host in report.drifted:
            typer.echo(
                f"\n{host.host} would change {host.changed} task(s). "
                "Run 'blitzecdn deploy' to converge it.",
                err=True,
            )
        for host in report.unreachable:
            typer.echo(
                f"\n{host.host} could not be reached, so nothing is known about "
                "its configuration.",
                err=True,
            )
    if not report.in_sync:
        raise typer.Exit(ExitCode.DRIFT_DETECTED)


@app.command()
def status(
    deployment_id: Annotated[str | None, typer.Argument()] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one deployment or recent deployment history."""
    repository = _control_plane().repository
    value = (
        repository.get_deployment(deployment_id)
        if deployment_id
        else repository.list_deployments(limit)
    )
    _emit(value, json_output=json_output)


@app.command()
def audit(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show immutable operator audit events."""
    _emit(_control_plane().repository.list_audit_events(limit), json_output=json_output)


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    resolver_check: Annotated[
        bool,
        typer.Option(
            "--resolver/--no-resolver",
            help="Probe the resolver for invented answers (one DNS query).",
        ),
    ] = True,
) -> None:
    """Report local readiness without contacting the edge servers.

    The resolver probe is the one thing here that leaves the machine: a single
    lookup of a reserved name that must not exist. It earns its place because a
    resolver that answers it is invisible to every other check while making all
    of them wrong. Pass --no-resolver on a host with no DNS at all.
    """
    settings = _settings()
    # Certificate expiry is read from the local store, so it belongs in a
    # check that promises not to touch the network. It is also the thing most
    # likely to take a site down while every other check stays green.
    expiring = _control_plane().expiring_certificates()
    report = {
        "python_supported": True,
        "state_dir": str(settings.state_dir),
        "api_auth_configured": bool(settings.api_keys),
        "configuration_errors": settings.validate_runtime(),
        "certificates_expiring": [
            {
                "site": status.site,
                "days_remaining": status.days_remaining,
                "renewable": status.renewable,
            }
            for status in expiring
        ],
    }
    resolver_report = check_resolver(settings) if resolver_check else None
    if resolver_report is not None:
        report["resolver"] = {
            "passed": resolver_report.passed,
            "detail": resolver_report.detail,
        }
    _emit(report, json_output=json_output)
    if resolver_report is not None and not resolver_report.passed:
        typer.echo(f"\n{resolver_report.detail}.", err=True)
    if not json_output and expiring:
        typer.echo(
            f"\n{len(expiring)} certificate(s) expire within "
            f"{CERTIFICATE_RENEWAL_DAYS} days. Run 'blitzecdn cert renew'.",
            err=True,
        )
    if report["configuration_errors"]:
        raise typer.Exit(ExitCode.CONFIGURATION)


@cache_app.command("purge")
def cache_purge(
    url: Annotated[
        list[str] | None,
        typer.Option(
            "--url",
            help=(
                "Absolute URL to purge, e.g. https://cdn.example.com/app.js. "
                "Repeat the option to purge more than one."
            ),
        ),
    ] = None,
    everything: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Empty the cache entirely instead of removing named URLs.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Required confirmation for --all."),
    ] = False,
    limit: Annotated[str | None, _LIMIT_OPTION] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove cached responses from the edges.

    Every variant of a URL goes: nginx keys entries by method and by content
    encoding, so one URL is several entries and leaving any behind would keep
    serving the object to some clients.

    Purging changes no configuration and writes no desired state, so it needs
    no deploy afterwards and does not wait for one in progress.
    """
    if (
        everything
        and not yes
        and not typer.confirm(
            "Empty the cache on "
            + (f"edges matching {limit!r}" if limit else "every edge")
            + "? Every object will be re-fetched from its origin."
        )
    ):
        raise typer.Abort()
    entries = [_purge_entry(value) for value in url or []]
    result = _control_plane().purge_cache(
        "cli", entries=entries, purge_all=everything, host_limit=limit
    )
    _emit(result, json_output=json_output)
    if not json_output:
        if result.complete:
            typer.echo(f"\nPurged on all {len(result.hosts)} edges.")
        for host in result.failed:
            typer.echo(
                f"\n{host.host} did not purge, so it may still be serving the "
                "cached copy. Re-run once it is reachable.",
                err=True,
            )
    if not result.complete:
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


def _purge_entry(value: str) -> PurgeEntry:
    """Read an operator-supplied URL into the host and path the cache keys on."""
    parsed = urlsplit(value if "//" in value else f"https://{value}")
    if not parsed.hostname:
        raise typer.BadParameter(f"{value!r} has no hostname")
    if parsed.scheme not in {"http", "https"}:
        raise typer.BadParameter(f"{value!r} must be http or https")
    # The query string is part of $request_uri and therefore part of the cache
    # key, so it is kept: purging '/a' must not silently purge '/a?v=2'.
    uri = parsed.path or "/"
    if parsed.query:
        uri = f"{uri}?{parsed.query}"
    return PurgeEntry(host=parsed.hostname, uri=uri, scheme=OriginScheme(parsed.scheme))


@app.command()
def stats(
    limit: Annotated[str | None, _LIMIT_OPTION] = None,
    by_site: Annotated[
        bool,
        typer.Option("--by-site", help="Break the numbers down by virtual host."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report cache effectiveness across the edges.

    Reads the access log and nginx's own counters on each edge. Changes
    nothing, so it is safe to run at any time, including during a deploy.

    The hit ratio counts only requests that consulted the cache. Redirects,
    errors nginx served itself, and sites with caching disabled are excluded
    rather than scored as misses.
    """
    report = _control_plane().cache_stats("cli", host_limit=limit)
    if json_output:
        _emit(_stats_document(report, by_site=by_site), json_output=True)
        return
    _emit(_stats_document(report, by_site=by_site), json_output=False)
    for edge in report.silent:
        typer.echo(f"\n{edge.host} reported nothing: {edge.error}", err=True)
    if report.hit_ratio is None:
        typer.echo(
            "\nNo cacheable requests in the window read, so there is no hit "
            "ratio yet. A fresh edge or a quiet one both look like this."
        )


def _stats_document(report: CacheStatsReport, *, by_site: bool) -> dict[str, Any]:
    """One shape for both outputs, so --json is never a different answer."""
    document: dict[str, Any] = {
        "collected_at": report.collected_at.isoformat(),
        "hit_ratio": report.hit_ratio,
        "hits": report.hits,
        "cacheable_requests": report.cacheable_requests,
        "requests": report.requests,
        "edges": [
            {
                "host": edge.host,
                "hit_ratio": edge.hit_ratio,
                "requests": edge.requests,
                "nginx_reachable": edge.nginx_reachable,
                "connections": edge.connections,
                "error": edge.error,
            }
            for edge in report.edges
        ],
    }
    if by_site:
        document["sites"] = [
            {
                "site": site.site,
                "hit_ratio": site.hit_ratio,
                "hits": site.hits,
                "cacheable_requests": site.cacheable_requests,
                "requests": site.requests,
            }
            for site in report.by_site()
        ]
    return document


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
) -> None:
    """Run the authenticated HTTP control plane."""
    settings = _settings()
    if not settings.api_keys:
        raise typer.BadParameter("configure BLITZE_API_KEYS before starting the API")
    uvicorn.run(create_app(settings), host=host, port=port, access_log=True)


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the virtual hosts the edges will serve.

    Derived, not created: only proxied records appear. A record you proxied but
    have not deployed is already listed here — this is desired state, not what
    the fleet is running. Use 'drift' for that.
    """
    _emit(_control_plane().repository.list_sites(), json_output=json_output)


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
    _emit(_control_plane().repository.get_site(name), json_output=json_output)


@domain_app.command("add")
def domain_add(
    name: Annotated[str, typer.Argument(help="Zone to serve, e.g. example.com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Register a DNS zone delegated to BlitzeCDN."""
    _emit(
        _control_plane().create_domain(Domain(name=name), "cli"),
        json_output=json_output,
    )


@domain_app.command("list")
def domain_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the DNS zones delegated to BlitzeCDN."""
    _emit(_control_plane().list_domains(), json_output=json_output)


@domain_app.command("remove")
def domain_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Remove a zone and every record in it."""
    if not yes and not typer.confirm(f"Delete {name!r} and all of its records?"):
        raise typer.Abort()
    _control_plane().delete_domain(name, "cli")
    typer.echo(f"Deleted {name}")


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
    _emit(_control_plane().create_record(record, "cli"), json_output=json_output)


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
    _emit(_control_plane().list_records(domain), json_output=json_output)


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
    record = _control_plane().set_proxied(domain, name, type_, on, "cli")
    _emit(record, json_output=json_output)
    if not json_output:
        typer.echo(
            f"{record.fqdn} is now "
            f"{'proxied through the CDN' if on else 'bypassing the CDN'}. "
            "Run 'blitzecdn deploy' to apply, and make sure DNS points at "
            f"{'an edge' if on else record.value}."
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
    control = _control_plane()
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
        current = control.get_record(domain, name, type_).firewall
        firewall = SiteFirewall.model_validate(current.model_dump() | named)
    record = control.update_record(
        domain, name, type_, RecordPatch(firewall=firewall), "cli"
    )
    _emit(record, json_output=json_output)
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
    _control_plane().delete_record(domain, name, type_, "cli")
    typer.echo(f"Deleted {label}")


@dns_app.command("export")
def dns_export(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Emit every record for the system that publishes DNS.

    Proxied records carry no address: they must resolve to an edge, and edge
    addressing is owned by the DNS system rather than the control plane.
    """
    _emit(_control_plane().dns_export(), json_output=json_output)


@origin_app.command("check")
def origin_check(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Connect to every enabled site's origin the way the edge will.

    Catches the misconfigurations `validate` cannot see — an origin that does
    not resolve, is not listening, or presents a certificate that does not
    match the SNI the edge will send. Exits 3 if any origin fails.

    This runs from the controller, not from an edge, so a pass is good evidence
    rather than proof. A failure is almost always real.
    """
    results = _control_plane().check_origins()
    _emit(results, json_output=json_output)
    failures = [result for result in results if not result.ok]
    if not json_output:
        if not results:
            typer.echo("No enabled sites to check.")
        elif not failures:
            typer.echo(f"\nAll {len(results)} origins answered as expected.")
        for failure in failures:
            typer.echo(
                f"\n{failure.site} ({failure.origin}): {failure.detail}", err=True
            )
    if failures:
        raise typer.Exit(ExitCode.CONFIGURATION)


@cert_app.command("list")
def cert_list(
    expiring_in: Annotated[
        int | None,
        typer.Option(
            "--expiring-in",
            help="Show only certificates with at most this many days left.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List managed certificates, soonest expiry first.

    Exits 4 if any listed certificate has already expired, so a scheduled
    check notices without anyone reading the output.
    """
    control = _control_plane()
    statuses = (
        control.expiring_certificates(expiring_in)
        if expiring_in is not None
        else control.certificate_statuses()
    )
    _emit(statuses, json_output=json_output)
    if not json_output and not statuses:
        typer.echo("No managed certificates.")
    if any(status.expired for status in statuses):
        raise typer.Exit(ExitCode.CONFLICT)


@cert_app.command("preflight")
def cert_preflight(
    name: Annotated[str, typer.Argument(help="Site name.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check whether a certificate could be issued for a site right now.

    Looks at what issuance actually depends on and the control plane cannot
    control: that the hostname resolves to one of our edges, that CAA permits
    our CA, that the vhost is already deployed, plus advisories on the origin
    and the record's TTL. Contacts no CA and changes nothing, so it is safe to
    run repeatedly while a customer is still moving their DNS.

    Exits 3 if anything blocks issuance. Advisories alone do not.
    """
    report = _control_plane().certificate_preflight(name)
    _emit(report, json_output=json_output)
    if not json_output:
        for check in report.checks:
            mark = "ok  " if check.passed else f"{check.severity.value.upper()}"
            typer.echo(f"  {mark:9} {check.name}: {check.detail}")
        if report.ok:
            typer.echo(f"\n{name} is ready for issuance.")
        else:
            typer.echo(f"\n{name} cannot be issued yet: {report.summary()}", err=True)
    if not report.ok:
        raise typer.Exit(ExitCode.CONFIGURATION)


@cert_app.command("renew")
def cert_renew(
    expiring_in: Annotated[
        int,
        typer.Option(
            "--expiring-in", help="Renew certificates with at most this many days left."
        ),
    ] = CERTIFICATE_RENEWAL_DAYS,
    force: Annotated[
        bool,
        typer.Option("--force", help="Renew every ACME certificate regardless of age."),
    ] = False,
    site: Annotated[
        list[str] | None,
        typer.Option(
            "--site",
            help=(
                "Renew only this site; repeat the option to add more. "
                "Without it every managed certificate is considered."
            ),
        ),
    ] = None,
    deploy_after: Annotated[
        bool,
        typer.Option(
            "--deploy",
            help="Deploy once after successful renewals so edges receive them.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reissue ACME certificates that are close to expiry.

    Safe to run on a schedule: a certificate that is not yet due is left
    alone, and one site failing does not stop the others. Renewed
    certificates reach the edges on the next deploy.

    Use --site to retry a single failure without sending every other
    subscription back to the CA, which is rate limited.
    """
    result = _control_plane().renew_certificates(
        "cli", within_days=expiring_in, force=force, sites=site or None
    )
    deployment = None
    if deploy_after and result["renewed"] and not result["failed"]:
        deployment = _control_plane().deploy("cli")
    output: dict[str, Any] = result
    if deploy_after:
        output = {
            **result,
            "deployment": (
                deployment.model_dump(mode="json") if deployment is not None else None
            ),
        }
    _emit(output, json_output=json_output)
    # Only the prose summary is suppressed under --json. The exit code below
    # still has to fire: --json is the scheduled-run path, and that is exactly
    # the caller that has nothing but the exit code to alert on.
    if not json_output:
        if result["renewed"] and not deploy_after:
            typer.echo(
                f"\nRenewed {len(result['renewed'])} certificate(s). Run "
                "'blitzecdn deploy' to install them on the edges."
            )
        for problem in result["skipped"]:
            typer.echo(f"  - {problem}", err=True)
        for problem in result["failed"]:
            typer.echo(f"  - renewal failed: {problem}", err=True)
    if result["failed"] or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@cert_app.command("reconcile")
def cert_reconcile(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Issue ready first certificates and deploy them to the edge fleet.

    Safe to schedule frequently: sites with certificates are ignored, blocked
    preflights never contact the CA, and the deployment runs only after at
    least one new certificate was issued.
    """
    result = _control_plane().reconcile_certificates("cli")
    deployment = result["deployment"]
    if deployment is not None and not isinstance(deployment, Deployment):
        raise RuntimeError("certificate reconciliation returned an invalid deployment")
    document = {
        **result,
        "deployment": (
            deployment.model_dump(mode="json") if deployment is not None else None
        ),
    }
    _emit(document, json_output=json_output)
    if result["failed"] or (
        deployment is not None and deployment.status is not DeploymentStatus.SUCCEEDED
    ):
        raise typer.Exit(ExitCode.DEPLOYMENT_FAILED)


@edge_app.command("list")
def edge_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List configured edge servers."""
    settings = _settings()
    _emit(Inventory(settings.inventory_path).list_edges(), json_output=json_output)


@edge_app.command("add")
def edge_add(
    name: Annotated[str, typer.Argument(help="Stable edge name.")],
    host: Annotated[str, typer.Option("--host", help="SSH hostname or address.")],
    ssh_source: Annotated[
        list[str],
        typer.Option(
            "--ssh-source",
            help="Trusted management CIDR; repeat the option to add more.",
        ),
    ],
    public_address: Annotated[
        list[str] | None,
        typer.Option(
            "--public-address",
            help=(
                "Public IP or hostname serving CDN traffic; repeat for NAT or "
                "multi-address edges. Defaults to --host."
            ),
        ),
    ] = None,
    user: Annotated[str, typer.Option("--user", help="Non-root SSH user.")] = "deploy",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Add an edge while preserving fail-closed firewall policy."""
    if not ssh_source:
        raise typer.BadParameter(
            "at least one --ssh-source management CIDR is required"
        )
    settings = _settings()
    edge = Inventory(settings.inventory_path).add_edge(
        name,
        host=host,
        user=user,
        ssh_sources=ssh_source,
        public_addresses=public_address or [],
    )
    _emit(edge, json_output=json_output)


@edge_app.command("update")
def edge_update(
    name: Annotated[str, typer.Argument(help="Stable edge name.")],
    public_address: Annotated[
        list[str],
        typer.Option(
            "--public-address",
            help="Replacement public CDN IP or hostname; repeat when needed.",
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Replace the public addresses used for DNS and certificate checks."""
    if not public_address:
        raise typer.BadParameter("at least one --public-address is required")
    settings = _settings()
    edge = Inventory(settings.inventory_path).set_public_addresses(name, public_address)
    _emit(edge, json_output=json_output)


@edge_app.command("remove")
def edge_remove(
    name: str,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    decommission: Annotated[
        bool,
        typer.Option(
            "--decommission/--no-decommission",
            help="Strip BlitzeCDN configuration and TLS keys from the host first.",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove the entry even if the teardown failed. For a host that "
            "no longer exists.",
        ),
    ] = False,
) -> None:
    """Decommission an edge and remove it from desired state.

    Once the inventory entry is gone the host is unaddressable, so the teardown
    has to happen first. ``--no-decommission`` skips it for a host that was
    already wiped by other means; the files it would have removed, including
    private keys, then stay where they are.
    """
    prompt = (
        f"Remove BlitzeCDN configuration and TLS keys from {name!r}, then stop "
        "managing it?"
        if decommission
        else f"Stop managing edge {name!r} without cleaning it up?"
    )
    if not yes and not typer.confirm(prompt):
        raise typer.Abort()
    if decommission:
        _control_plane().decommission_edge(name, "cli", force=force)
    else:
        Inventory(_settings().inventory_path).remove_edge(name)
    typer.echo(f"Removed {name}")


def run() -> None:
    try:
        app()
    except (BlitzeError, ValidationError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        # SystemExit, not typer.Exit: we are outside Click's invocation by the
        # time app() has raised, so a typer.Exit here is nothing but an
        # unhandled exception and prints a traceback over the message above.
        raise SystemExit(ExitCode.INVALID_INPUT) from exc


if __name__ == "__main__":
    run()
