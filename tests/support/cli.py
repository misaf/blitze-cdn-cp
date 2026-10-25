"""Helpers shared by the command-line modules.

The Typer runner, and the builders that stand up a control plane with a seeded
zone behind it. Helpers used by one module stay beside its tests.
"""

import pytest
from typer.testing import CliRunner

from blitzecdn.composition import ApplicationServices, FleetRunner, Repository
from blitzecdn.core.config import Settings
from blitzecdn.entrypoints.cli import main as cli
from support.control_plane import (
    cli_control_plane,
    repository_on,
)

runner = CliRunner()


def _seed_site(
    control: ApplicationServices,
    label: str = "api",
    origin: str = "198.51.100.20",
    name: str | None = None,
) -> str:
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


def _control(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    runner_double: FleetRunner | None = None,
) -> ApplicationServices:
    return cli_control_plane(settings, monkeypatch, runner_double)


def _store(settings: Settings) -> Repository:
    return repository_on(settings)
