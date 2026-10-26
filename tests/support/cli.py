"""Helpers shared by the command-line modules.

The Typer runner, and the builders that stand up a control plane with a seeded
zone behind it. Helpers used by one module stay beside its tests.
"""

import pytest
from typer.testing import CliRunner

from blitzecdn.composition import FleetRunner, Repository
from blitzecdn.core.config import Settings
from blitzecdn.entrypoints.cli import main as cli
from support.control_plane import (
    ControlPlane,
    cli_control_plane,
    repository_on,
)

runner = CliRunner()


def _seed_site(
    control: ControlPlane,
    label: str = "api",
    origin: str = "198.51.100.20",
    name: str | None = None,
) -> str:
    """One proxied hostname in a zone, through the CLI.

    The record's value is the origin while proxied and the DNS answer when not.
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
) -> ControlPlane:
    return cli_control_plane(settings, monkeypatch, runner_double)


def _store(settings: Settings) -> Repository:
    return repository_on(settings)
