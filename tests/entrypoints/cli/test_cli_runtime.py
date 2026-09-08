"""Bootstrapping: setup, the composition root, and refusing to serve unauthenticated."""

import sys

import pytest
from cli_support import (
    runner,
)
from control_plane_fixtures import (
    FakeRunner,
)

from blitzecdn.capabilities.diagnostics import cli as diagnostics_cli
from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository


def test_schema_only_setup_initializes_database_without_scaffolding(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["setup", "--schema-only"])

    assert result.exit_code == 0
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    assert result.stdout == ""


def test_run_reports_domain_errors_without_a_traceback(settings, monkeypatch, capsys):
    """`run()` is outside Click, so it must exit rather than raise."""
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "record", "list", "absent.example"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    # NOT_FOUND rather than INVALID_INPUT: the API answers 404 here, and a
    # caller driving the CLI needs the same distinction. Everything used to
    # exit 2, so "no such zone" and "bad flag" were the same answer.
    assert exit_info.value.code == cli.ExitCode.NOT_FOUND
    assert "does not exist" in capsys.readouterr().err


def test_control_plane_factory_builds_from_the_environment(settings, monkeypatch):
    """The two factories are the only wiring between Settings and the commands."""
    monkeypatch.setattr(
        cli.Settings, "from_environment", classmethod(lambda cls: settings)
    )
    assert cli.common.settings() is settings
    assert cli.common.control_plane().settings is settings


def test_serve_refuses_to_start_unauthenticated(settings, monkeypatch):
    unauthenticated = settings.model_copy(update={"api_keys": {}})
    monkeypatch.setattr(cli.common, "settings", lambda: unauthenticated)
    started = []
    monkeypatch.setattr(
        diagnostics_cli.uvicorn, "run", lambda *a, **k: started.append(a)
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code != 0
    assert started == []
