"""`zone firewall`: which lists an option replaces, and what it refuses."""

import json

from support.cli import (
    runner,
    seed_cli_site,
)
from support.control_plane import (
    FakeRunner,
    build_application,
)
from support.stores import Stores

from blitzecdn.entrypoints.cli import main as cli


def test_cli_firewall_replaces_only_the_lists_it_names(settings, monkeypatch):
    """Merge semantics, and the derived site carries the result.

    Unlike the API PATCH, the CLI keeps the lists the operator did not mention.
    """
    control = build_application(
        settings=settings,
        repository=Stores(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    seed_cli_site(control, "api", "198.51.100.20")

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
        repository=Stores(settings.database_path),
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
        repository=Stores(settings.database_path),
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


def test_cli_firewall_sets_method_and_path_allowlists(settings, monkeypatch):
    control = build_application(
        settings=settings,
        repository=Stores(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    seed_cli_site(control, "api", "198.51.100.20")

    result = runner.invoke(
        cli.app,
        [
            "domain",
            "firewall",
            "example.com",
            "--allow-method",
            "get",
            "--allow-path",
            "/api/",
            "--deny-path",
            "/api/admin",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    firewall = json.loads(result.stdout)["firewall"]
    assert firewall["allowed_methods"] == ["GET", "HEAD"]
    assert firewall["allowed_paths"] == ["/api/"]
    assert firewall["denied_paths"] == ["/api/admin"]

    # An allowlist and a denylist of methods contradict each other.
    conflicting = runner.invoke(
        cli.app, ["domain", "firewall", "example.com", "--deny-method", "DELETE"]
    )
    assert conflicting.exit_code != 0


def test_cli_firewall_switches_case_insensitive_path_matching(settings, monkeypatch):
    control = build_application(
        settings=settings,
        repository=Stores(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    seed_cli_site(control, "api", "198.51.100.20")

    on = runner.invoke(
        cli.app,
        [
            "domain",
            "firewall",
            "example.com",
            "--deny-path",
            "/admin",
            "--case-insensitive-paths",
            "--json",
        ],
    )
    assert on.exit_code == 0, on.output
    firewall = json.loads(on.stdout)["firewall"]
    assert firewall["case_insensitive_paths"] is True
    assert firewall["denied_paths"] == ["/admin"]

    # The flag on its own is an edit, and it keeps the lists it does not name.
    off = runner.invoke(
        cli.app,
        ["domain", "firewall", "example.com", "--case-sensitive-paths", "--json"],
    )
    assert off.exit_code == 0, off.output
    firewall = json.loads(off.stdout)["firewall"]
    assert firewall["case_insensitive_paths"] is False
    assert firewall["denied_paths"] == ["/admin"]
