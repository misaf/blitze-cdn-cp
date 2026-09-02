"""`blitzecdn origin check`, from the command line.

These three moved here from the control plane's CLI suite with the command
itself. They belong here twice over: the command exists only while this
distribution is installed, so a core-only run of the control plane's suite
would fail on a surface core no longer contributes.

What the exit codes mean is the contract worth holding. 0 with a silent edge is
deliberate — an edge that said nothing has not reported a *failure*, and an
operator scripting this must be able to tell "an origin is unreachable" from
"one edge did not answer".
"""

from __future__ import annotations

from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    cli_control_plane,
    host_run,
    origin_report,
    repository_on,
)
from typer.testing import CliRunner

from blitzecdn.cli import main as cli
from blitzecdn.features.sites.domain import CdnSite

runner = CliRunner()


def _seed_origin_site(settings):
    repository_on(settings).sites.create_site(
        CdnSite.model_validate(
            {
                "name": "cdn-example-com",
                "server_names": ["cdn.example.com"],
                "origin_host": "origin.example.com",
            }
        )
    )


def test_origin_check_names_the_edges_that_could_not_reach_an_origin(
    settings, monkeypatch
):
    """The fleet answers, so the answer is per edge as well as per site.

    An origin no edge can reach is down; one only some edges can reach is a
    routing or allow-list problem. Reporting which edges failed is what lets an
    operator tell those apart, and is the whole reason the check moved off the
    controller.
    """
    cli_control_plane(
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
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "cdn-example-com: unreachable from edge-b" in result.output
    assert "edge-a" not in result.output.split("unreachable from")[1]


def test_origin_check_passes_when_every_edge_reaches_every_origin(
    settings, monkeypatch
):
    cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))]),
    )
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "answered as expected" in result.output


def test_origin_check_reports_an_edge_that_said_nothing(settings, monkeypatch):
    """A silent edge is not a passing edge."""
    cli_control_plane(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(host_run("edge-a", unreachable=1))]),
    )
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "edge-a: unreachable" in result.output
