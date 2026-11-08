"""`blitzecdn cache purge`, `stats` and `connections`, as an operator types them.

Reached through the real root application, so these also hold that the
commands are on the tree.
"""

import json
import sys
from datetime import UTC, datetime

import pytest
from support.cache import purged_prefixes, purges
from support.control_plane import (
    FakeRunner,
    ansible_run,
    cli_control_plane,
    host_run,
    seed_site,
)
from support.stores import Stores
from typer.testing import CliRunner

from blitzecdn.capabilities.cache.domain import PurgeEntry, PurgePrefix
from blitzecdn.capabilities.traffic.domain import (
    CollectedUsage,
    HourlyUsage,
    LogCursor,
    UsageBatch,
)
from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()


def _purge_ok():
    return ansible_run(host_run("edge-a", changed=1))


def _purgeable_site(control):
    """A site serving TLS, because these tests purge `https://` URLs.

    The scheme leads the cache key, so a purge for an unserved scheme is refused.
    """
    return seed_site(
        control,
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/cdn.pem",
        certificate_key_path="/etc/ssl/private/cdn.key",
    )


def test_cache_purge_sends_the_url_split_into_host_and_uri(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    control = cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(control)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/app.js"]
    )

    assert result.exit_code == 0
    assert purges(fake)[0][0] == (
        PurgeEntry(host="cdn.example.com", uri="/app.js", scheme="https"),
    )


def test_cache_purge_by_prefix_and_hostname(settings, monkeypatch):
    """The scheme of a --prefix is ignored: a prefix purge covers both."""
    fake = FakeRunner([_purge_ok()])
    control = cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(control)

    result = runner.invoke(
        cli.app,
        [
            "cache",
            "purge",
            "--prefix",
            "http://cdn.example.com/blog/",
            "--hostname",
            "cdn.example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert purged_prefixes(fake) == [
        (
            PurgePrefix(host="cdn.example.com", prefix="/blog/"),
            PurgePrefix(host="cdn.example.com", prefix="/"),
        )
    ]
    assert purges(fake)[0][0] == ()


def test_cache_purge_refuses_a_prefix_without_a_hostname(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    cli_control_plane(settings, monkeypatch, fake)

    result = runner.invoke(cli.app, ["cache", "purge", "--prefix", "/blog/"])

    assert result.exit_code != 0
    assert purges(fake) == []


def test_cache_purge_keeps_the_query_string(settings, monkeypatch):
    """It is part of $request_uri, so '/a' and '/a?v=2' are different entries."""
    fake = FakeRunner([_purge_ok()])
    control = cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(control)

    runner.invoke(cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a?v=2"])

    assert purges(fake)[0][0][0].uri == "/a?v=2"


def test_cache_purge_defaults_a_bare_url_to_https_and_root(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    control = cli_control_plane(settings, monkeypatch, fake)
    _purgeable_site(control)

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
    control = cli_control_plane(settings, monkeypatch, FakeRunner([partial]))
    _purgeable_site(control)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a.js"]
    )

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert "edge-b did not purge" in result.output


def test_cache_purge_reports_an_unserved_host_without_a_traceback(
    settings, monkeypatch
):
    control = cli_control_plane(settings, monkeypatch, FakeRunner([_purge_ok()]))
    _purgeable_site(control)
    monkeypatch.setattr(
        sys,
        "argv",
        ["blitzecdn", "cache", "purge", "--url", "https://nope.example.com/x"],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.NOT_FOUND


def _seed_stats(settings):
    hour = datetime(2026, 10, 27, 9, tzinfo=UTC)
    repository = Stores(settings.database_path)
    repository.traffic.apply(
        "edge-a",
        CollectedUsage(
            batches=(
                UsageBatch(
                    end=LogCursor(stream_id="a" * 64, offset=100, anchor="f" * 64),
                    buckets=(
                        HourlyUsage(
                            hostname="cdn.example.com",
                            hour=hour,
                            requests=4,
                            bytes_sent=40,
                            body_bytes_sent=0,
                            cache={
                                "HIT": {"requests": 3, "bytes_sent": 30},
                                "MISS": {"requests": 1, "bytes_sent": 10},
                            },
                        ),
                    ),
                ),
            )
        ),
    )
    repository.close()


def test_stats_reads_stored_hourly_outcomes(settings, monkeypatch):
    fake = FakeRunner([])
    cli_control_plane(settings, monkeypatch, fake)
    _seed_stats(settings)

    result = runner.invoke(
        cli.app,
        [
            "cache",
            "stats",
            "--start",
            "2026-10-27T09:00:00Z",
            "--end",
            "2026-10-27T10:00:00Z",
            "--hostname",
            "cdn.example.com",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total"] == 1
    (row,) = payload["items"]
    assert row["edge"] == "edge-a"
    assert row["hit_ratio"] == 0.75
    assert row["byte_hit_ratio"] == 0.75
    assert row["outcomes"]["HIT"] == {"requests": 3, "bytes_sent": 30}
    assert fake.playbooks == []


def test_connections_names_an_edge_that_reported_nothing(settings, monkeypatch):
    cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner(
            [
                ansible_run(
                    host_run(
                        "edge-a",
                        ok=5,
                        report={"nginx_reachable": True, "connections": {"active": 3}},
                    ),
                    host_run("edge-b", ok=0, unreachable=1),
                )
            ]
        ),
    )

    result = runner.invoke(cli.app, ["cache", "connections"])

    assert result.exit_code == 0, result.output
    assert "edge-b reported nothing: unreachable" in result.output
