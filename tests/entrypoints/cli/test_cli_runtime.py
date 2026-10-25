"""Bootstrapping: setup, the composition root, and refusing to serve unauthenticated."""

import sys

import pytest
from cli_support import (
    runner,
)
from control_plane_fixtures import (
    FakeRunner,
    build_application,
)

from blitzecdn.capabilities.diagnostics import cli as diagnostics_cli
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli


def test_migrate_initializes_database_without_scaffolding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 0
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    assert result.stdout == "Database schema is current.\n"
    assert not (tmp_path / ".env").exists()


def test_run_reports_domain_errors_without_a_traceback(settings, monkeypatch, capsys):
    """`run()` is outside Click, so it must exit rather than raise."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "services", lambda: control)
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
    assert cli.common.services().settings is settings


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


def test_one_invocation_builds_its_services_once_and_closes_them(settings, monkeypatch):
    """Asking twice inside a command reuses one graph; exiting releases it."""
    import typer

    built = []

    class Built:
        closed = False

        def close(self):
            self.closed = True

    def build(_services):
        built.append(Built())
        return built[-1]

    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(cli.common.Infrastructure, "build", lambda *_a, **_k: None)
    monkeypatch.setattr(cli.common, "build_services", lambda _infra: None)
    monkeypatch.setattr(cli.common, "build_cli", build)
    seen = []
    app = typer.Typer()

    @app.callback()
    def root(context: typer.Context) -> None:
        cli.common.scope_services(context)

    @app.command()
    def twice() -> None:
        seen.append(cli.common.services() is cli.common.services())
        seen.append(built[0].closed)

    assert runner.invoke(app, ["twice"]).exit_code == 0
    assert seen == [True, False]
    assert len(built) == 1
    assert built[0].closed
