"""Records: proxying a hostname on and off the edge, and removals."""

import json

from cli_support import (
    _control,
    _seed_site,
    _store,
    runner,
)
from control_plane_fixtures import (
    FakeRunner,
)

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain
from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository


def test_cli_proxy_and_unproxy_move_a_hostname_on_and_off_the_edge(
    settings, monkeypatch
):
    """`record proxy` / `record unproxy` is the CDN switch for one hostname.

    The site outlives the switch. Switching a record's proxy flag used to take
    the whole virtual host away, policy included, because the record carried
    the policy; here it takes the hostname on and off a site whose settings
    stay configured.
    """
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    added = runner.invoke(
        cli.app,
        [
            "record",
            "add",
            "example.com",
            "api",
            "--value",
            "198.51.100.20",
            "--no-proxy",
            "--json",
        ],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["proxied"] is False
    assert json.loads(added.stdout)["value"] == "198.51.100.20"
    # Unproxied, the zone derives no virtual host at all.
    assert control.dns.list_sites() == []

    proxied = runner.invoke(
        cli.app, ["record", "proxy", "example.com", "api", "--json"]
    )
    assert proxied.exit_code == 0
    sites = json.loads(runner.invoke(cli.app, ["domain", "hosts", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [["api.example.com"]]
    assert sites[0]["origin_host"] == "198.51.100.20"

    runner.invoke(
        cli.app,
        ["record", "unproxy", "example.com", "api", "--value", "203.0.113.7"],
    )
    # Off the edge, the zone derives nothing again.
    assert (
        json.loads(runner.invoke(cli.app, ["domain", "hosts", "--json"]).stdout) == []
    )


def test_cli_dns_export_hides_addresses_for_proxied_records(settings, monkeypatch):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")
    exported = json.loads(runner.invoke(cli.app, ["dns", "export", "--json"]).stdout)
    assert exported[0]["proxied"] is True
    assert "value" not in exported[0]


def test_domain_remove_keeps_the_zone_when_the_operator_declines(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "cli")

    result = runner.invoke(cli.app, ["domain", "remove", "example.com"], input="n\n")

    assert result.exit_code == 1
    assert [domain.name for domain in control.dns.list_domains()] == ["example.com"]


def test_record_remove_keeps_the_record_when_the_operator_declines(
    settings, monkeypatch
):
    control = _control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "cli")
    control.dns.create_record(
        DnsRecord(
            domain="example.com",
            name="cdn",
            proxied=False,
            value="198.51.100.10",
        ),
        "cli",
    )

    result = runner.invoke(
        cli.app, ["record", "remove", "example.com", "cdn"], input="n\n"
    )

    assert result.exit_code == 1
    assert len(_store(settings).zones.list_records("example.com")) == 1
