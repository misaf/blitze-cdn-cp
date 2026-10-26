"""`zone firewall`: which lists an option replaces, and what it refuses."""

import json

from support.cli import (
    _seed_site,
    runner,
)
from support.control_plane import (
    FakeRunner,
    build_application,
)

from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli


def test_cli_firewall_replaces_only_the_lists_it_names(settings, monkeypatch):
    """Merge semantics, and the derived site carries the result.

    Unlike the API PATCH, the CLI keeps the lists the operator did not mention.
    """
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")

    first = runner.invoke(
        cli.app,
        [
            "domain",
            "firewall",
            "example.com",
            "--deny-source",
            "203.0.113.0/24",
            "--deny-path",
            "/admin",
            "--json",
        ],
    )
    assert first.exit_code == 0

    # Names only the country list; the source and path rules must survive.
    second = runner.invoke(
        cli.app,
        ["domain", "firewall", "example.com", "--deny-country", "ru", "--json"],
    )
    assert second.exit_code == 0
    firewall = json.loads(second.stdout)["firewall"]
    assert firewall["deny_sources"] == ["203.0.113.0/24"]
    assert firewall["denied_paths"] == ["/admin"]
    assert firewall["denied_countries"] == ["RU"]

    sites = json.loads(runner.invoke(cli.app, ["domain", "list", "--json"]).stdout)
    assert sites[0]["firewall"]["denied_countries"] == ["RU"]

    cleared = runner.invoke(
        cli.app, ["domain", "firewall", "example.com", "--clear", "--json"]
    )
    assert json.loads(cleared.stdout)["firewall"]["deny_sources"] == []


def test_cli_firewall_refuses_a_network_with_host_bits_set(settings, monkeypatch):
    """203.0.113.5/24 means one address to the operator and 256 to nginx."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20"],
    )
    result = runner.invoke(
        cli.app,
        [
            "domain",
            "firewall",
            "example.com",
            "--deny-source",
            "203.0.113.5/24",
        ],
    )
    assert result.exit_code != 0


def test_cli_firewall_requires_a_rule_or_clear(settings, monkeypatch):
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app, ["record", "add", "example.com", "api", "--value", "198.51.100.20"]
    )
    bare = runner.invoke(cli.app, ["domain", "firewall", "api-example-com"])
    assert bare.exit_code != 0
    conflicting = runner.invoke(
        cli.app,
        [
            "domain",
            "firewall",
            "example.com",
            "--clear",
            "--deny-path",
            "/admin",
        ],
    )
    assert conflicting.exit_code != 0
