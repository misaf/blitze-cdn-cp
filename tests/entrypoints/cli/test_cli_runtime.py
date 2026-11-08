"""Bootstrapping: setup, the composition root, and refusing to serve unauthenticated."""

import os
import sys

import pytest
from support.cli import (
    runner,
)
from support.control_plane import (
    FakeRunner,
    build_application,
)

from blitzecdn.capabilities.diagnostics import cli as diagnostics_cli
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli
from blitzecdn.entrypoints.cli import setup as setup_commands


def test_migrate_initializes_database_without_scaffolding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["migrate"])

    assert result.exit_code == 0
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    assert result.stdout == "Database schema is current.\n"
    assert not (tmp_path / ".env").exists()


def test_setup_writes_the_environment_file_named_by_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "config" / "controller.env"
    target.parent.mkdir()

    result = runner.invoke(cli.app, ["setup", "--env-file", str(target)])

    assert result.exit_code == 0
    assert f"Created: {target}" in result.stdout
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert target.read_text(encoding="utf-8").startswith("BLITZE_API_KEYS=local:")
    assert not (tmp_path / ".env").exists()


def test_setup_preserves_an_existing_env_file_or_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "existing.env"
    existing.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.env"
    link.symlink_to(tmp_path / "elsewhere")

    for target in (existing, link):
        result = runner.invoke(cli.app, ["setup", "--env-file", str(target)])
        assert result.exit_code == 0
        assert "already set up" in result.stdout

    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "elsewhere").exists()


def test_setup_never_follows_a_symlink_planted_before_the_create(tmp_path, monkeypatch):
    """A symlink appearing after settings load is left alone, not written through."""
    monkeypatch.chdir(tmp_path)
    real_migrate = setup_commands.migrate_database

    def migrate_then_plant(settings):
        real_migrate(settings)
        (tmp_path / ".env").symlink_to(tmp_path / "elsewhere")

    monkeypatch.setattr(setup_commands, "migrate_database", migrate_then_plant)

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0
    assert "already set up" in result.stdout
    assert not (tmp_path / "elsewhere").exists()


def test_setup_creates_a_private_environment_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["setup"])

    assert result.exit_code == 0
    assert "Created: .env" in result.stdout
    assert os.stat(tmp_path / ".env").st_mode & 0o777 == 0o600


def test_run_reports_domain_errors_without_a_traceback(settings, monkeypatch, capsys):
    """`run()` is outside Click, so it must exit rather than raise."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "container", lambda: control.container)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "record", "list", "absent.example"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    # NOT_FOUND rather than INVALID_INPUT, matching the API's 404.
    assert exit_info.value.code == cli.ExitCode.NOT_FOUND
    assert "does not exist" in capsys.readouterr().err


def test_control_plane_factory_builds_from_the_environment(settings, monkeypatch):
    """The two factories are the only wiring between Settings and the commands."""
    monkeypatch.setattr(
        cli.Settings, "from_environment", classmethod(lambda cls: settings)
    )
    assert cli.common.settings() is settings
    assert cli.common.service(cli.Settings) is settings


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

    def build(_settings):
        built.append(Built())
        return built[-1]

    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(cli.common, "cli_container", build)
    seen = []
    app = typer.Typer()

    @app.callback()
    def root(context: typer.Context) -> None:
        cli.common.scope_services(context)

    @app.command()
    def twice() -> None:
        seen.append(cli.common.container() is cli.common.container())
        seen.append(built[0].closed)

    assert runner.invoke(app, ["twice"]).exit_code == 0
    assert seen == [True, False]
    assert len(built) == 1
    assert built[0].closed
