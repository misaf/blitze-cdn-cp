"""`blitzecdn cache purge` and `blitzecdn stats`, as an operator types them.

These live with the distribution that contributes the commands. They were in
`tests/entrypoints/test_cli.py` while `cache` was a package inside the control
plane, which meant detaching the capability would have taken a chunk of the
core CLI suite with it — the package is the unit of modularity, so its tests
move with it and the core suite keeps only what core still owns.

The commands are reached through the real root `blitzecdn` application, not
through this package's Typer objects directly: what is being held is that
installing the distribution puts `cache purge` and `stats` on the command tree.
"""

import json
import sys

import pytest
from blitzecdn_cache.domain import PurgeEntry
from cache_support import purges
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    cli_control_plane,
    host_run,
    repository_on,
)
from typer.testing import CliRunner

from blitzecdn.cli import main as cli
from blitzecdn.features.sites.domain import CdnSite

runner = CliRunner()


def _purge_ok():
    return ansible_run(host_run("edge-a", changed=1))


def _purgeable_site(settings):
    """A site serving TLS, because these tests purge `https://` URLs.

    The scheme leads the cache key, so the control plane refuses a purge for a
    scheme the site never serves. A site without TLS caches nothing under
    https.
    """
    repository_on(settings).sites.create_site(
        CdnSite.model_validate(
            {
                "name": "cdn-example-com",
                "server_names": ["cdn.example.com"],
                "origin_host": "o.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/cdn.pem",
                "certificate_key_path": "/etc/ssl/private/cdn.key",
            }
        )
    )


def test_cache_purge_sends_the_url_split_into_host_and_uri(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(settings)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/app.js"]
    )

    assert result.exit_code == 0
    assert purges(fake)[0][0] == (
        PurgeEntry(host="cdn.example.com", uri="/app.js", scheme="https"),
    )


def test_cache_purge_keeps_the_query_string(settings, monkeypatch):
    """It is part of $request_uri, so '/a' and '/a?v=2' are different entries."""
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(settings)

    runner.invoke(cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a?v=2"])

    assert purges(fake)[0][0][0].uri == "/a?v=2"


def test_cache_purge_defaults_a_bare_url_to_https_and_root(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(settings)

    runner.invoke(cli.app, ["cache", "purge", "--url", "cdn.example.com"])

    assert purges(fake)[0][0] == (
        PurgeEntry(host="cdn.example.com", uri="/", scheme="https"),
    )


def test_cache_purge_rejects_a_url_with_no_hostname(settings, monkeypatch):
    cli_control_plane(settings, monkeypatch)
    result = runner.invoke(cli.app, ["cache", "purge", "--url", "https:///app.js"])
    assert result.exit_code != 0


def test_cache_purge_all_asks_before_emptying_the_cache(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)

    declined = runner.invoke(cli.app, ["cache", "purge", "--all"], input="n\n")

    assert declined.exit_code == 1
    assert purges(fake) == []


def test_cache_purge_all_proceeds_with_yes(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)

    result = runner.invoke(cli.app, ["cache", "purge", "--all", "--yes"])

    assert result.exit_code == 0
    assert purges(fake)[0][1] is True


def test_cache_purge_exits_five_when_an_edge_did_not_purge(settings, monkeypatch):
    """A partial purge must not read as success to a script."""
    partial = ansible_run(
        host_run("edge-a", changed=1), host_run("edge-b", ok=0, unreachable=1)
    )
    cli_control_plane(settings, monkeypatch, FakeRunner([partial]))
    _purgeable_site(settings)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a.js"]
    )

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert "edge-b did not purge" in result.output


def test_cache_purge_reports_an_unserved_host_without_a_traceback(
    settings, monkeypatch
):
    cli_control_plane(settings, monkeypatch, FakeRunner([_purge_ok()]))
    _purgeable_site(settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["blitzecdn", "cache", "purge", "--url", "https://nope.example.com/x"],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.NOT_FOUND


def _stats_runner(*hosts):
    """Counters arrive on the run itself, published by the role as a fact."""
    return FakeRunner([ansible_run(*hosts)])


_EDGE_REPORT = {
    "host": "edge-a",
    "collected_at": "2026-08-09T01:00:00Z",
    "nginx_reachable": True,
    "connections": {"active": 3},
    "cache": [
        {"site": "cdn.example.com", "outcome": "HIT", "requests": 3},
        {"site": "cdn.example.com", "outcome": "MISS", "requests": 1},
    ],
}


def test_stats_reports_the_fleet_hit_ratio(settings, monkeypatch):
    cli_control_plane(
        settings,
        monkeypatch,
        _stats_runner(host_run("edge-a", ok=5, report=_EDGE_REPORT)),
    )

    result = runner.invoke(cli.app, ["stats", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["hit_ratio"] == 0.75
    assert payload["edges"][0]["host"] == "edge-a"
    assert "sites" not in payload


def test_stats_by_site_breaks_the_numbers_down(settings, monkeypatch):
    cli_control_plane(
        settings,
        monkeypatch,
        _stats_runner(host_run("edge-a", ok=5, report=_EDGE_REPORT)),
    )

    payload = json.loads(
        runner.invoke(cli.app, ["stats", "--by-site", "--json"]).stdout
    )

    assert payload["sites"][0]["site"] == "cdn.example.com"
    assert payload["sites"][0]["hit_ratio"] == 0.75


def test_stats_says_so_when_there_is_no_cacheable_traffic_yet(settings, monkeypatch):
    """A fresh edge must not be reported as a cache that is failing."""
    quiet = {**_EDGE_REPORT, "cache": [{"site": "a", "outcome": "NONE", "requests": 4}]}
    cli_control_plane(
        settings, monkeypatch, _stats_runner(host_run("edge-a", ok=5, report=quiet))
    )

    result = runner.invoke(cli.app, ["stats"])

    assert result.exit_code == 0
    assert "no hit ratio yet" in result.output


def test_stats_names_an_edge_that_reported_nothing(settings, monkeypatch):
    cli_control_plane(
        settings,
        monkeypatch,
        _stats_runner(
            host_run("edge-a", ok=5, report=_EDGE_REPORT),
            host_run("edge-b", ok=0, unreachable=1),
        ),
    )

    result = runner.invoke(cli.app, ["stats"])

    assert "edge-b reported nothing: unreachable" in result.output
