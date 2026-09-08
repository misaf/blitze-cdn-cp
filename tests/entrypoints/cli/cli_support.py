"""Helpers shared by the command-line modules.

The Typer runner, and the builders that stand up a control plane with a seeded
zone behind it. Helpers used by one module stay beside its tests.
"""

from control_plane_fixtures import (
    cli_control_plane,
    repository_on,
)
from typer.testing import CliRunner

from blitzecdn.cli import main as cli

runner = CliRunner()


def _seed_site(control, label="api", origin="198.51.100.20", name=None):
    """One proxied hostname in a zone, through the CLI.

    A record names both the hostname and where the edge fetches from: its value
    is the origin while proxied and the DNS answer when not. The zone's serving
    policy is what the CLI edits around it.
    """
    assert (
        runner.invoke(
            cli.app,
            [
                "record",
                "add",
                "example.com",
                label,
                "--value",
                origin,
                "--proxy",
                "--json",
            ],
        ).exit_code
        == 0
    )
    return name or "example-com"


def _control(settings, monkeypatch, runner_double=None):
    return cli_control_plane(settings, monkeypatch, runner_double)


def _store(settings):
    return repository_on(settings)
