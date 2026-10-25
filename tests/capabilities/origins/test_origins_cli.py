"""`blitzecdn origin check`, from the command line.

The exit codes are the contract. 0 with a silent edge is deliberate: an edge
that said nothing reported no failure, so a script can tell an unreachable
origin from an edge that did not answer.
"""

from __future__ import annotations

from support.control_plane import (
    FakeRunner,
    ansible_run,
    cli_control_plane,
    host_run,
    origin_report,
    seed_site,
)
from typer.testing import CliRunner

from blitzecdn.entrypoints.cli import main as cli

runner = CliRunner()


def _seed_origin_site(control):
    """The site the origin check reports on, derived from a proxied record."""
    return seed_site(control)


def test_origin_check_names_the_edges_that_could_not_reach_an_origin(
    settings, monkeypatch
):
    """The fleet answers, so the answer is per edge as well as per site.

    No edge reaching an origin means it is down; only some means a routing or
    allow-list problem.
    """
    control = cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner(
            [
                ansible_run(
                    origin_report("edge-a"),
                    origin_report("edge-b", reachable=False, detail="timed out"),
                )
            ]
        ),
    )
    _seed_origin_site(control)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "example-com: unreachable from edge-b" in result.output
    assert "edge-a" not in result.output.split("unreachable from")[1]


def test_origin_check_passes_when_every_edge_reaches_every_origin(
    settings, monkeypatch
):
    control = cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))]),
    )
    _seed_origin_site(control)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "answered as expected" in result.output


def test_origin_check_reports_an_edge_that_said_nothing(settings, monkeypatch):
    """A silent edge is not a passing edge."""
    control = cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(host_run("edge-a", unreachable=1))]),
    )
    _seed_origin_site(control)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "edge-a: unreachable" in result.output
