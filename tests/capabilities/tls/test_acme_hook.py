"""`blitzecdn cert acme-hook`, the command certbot runs for an HTTP-01 challenge."""

from types import SimpleNamespace

from support.control_plane import ansible_run, host_run
from typer.testing import CliRunner

from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.entrypoints.cli import main as cli
from blitzecdn.entrypoints.cli.common import ExitCode

runner = CliRunner()

_CHALLENGE = {
    "CERTBOT_DOMAIN": "cdn.example.com",
    "CERTBOT_TOKEN": "safe_token",
    "CERTBOT_VALIDATION": "safe.validation",
}


def _fleet(monkeypatch, settings, run):
    calls = []

    class Fleet:
        def run_playbook(self, **values):
            calls.append(values)
            return run

    container = SimpleNamespace(get=lambda _kind: Fleet())
    monkeypatch.setattr(cli.common, "container", lambda: container)
    return calls


def test_the_hook_validates_and_runs_the_challenge_play(monkeypatch, settings):
    calls = _fleet(monkeypatch, settings, ansible_run(host_run("edge-a")))

    result = runner.invoke(cli.app, ["cert", "acme-hook", "present"], env=_CHALLENGE)

    assert result.exit_code == 0, result.output
    assert calls[0]["name"] == "acme-challenge"
    assert calls[0]["variables"]["blitzecdn_acme_action"] == "present"


def test_the_hook_fails_closed_before_any_play(monkeypatch, settings):
    calls = _fleet(monkeypatch, settings, ansible_run(host_run("edge-a")))

    bad_action = runner.invoke(cli.app, ["cert", "acme-hook", "bad"], env=_CHALLENGE)
    bad_environment = runner.invoke(
        cli.app,
        ["cert", "acme-hook", "present"],
        env=_CHALLENGE | {"CERTBOT_DOMAIN": "bad/domain", "CERTBOT_TOKEN": "bad token"},
    )

    assert bad_action.exit_code == ExitCode.INVALID_INPUT
    assert bad_environment.exit_code == ExitCode.INVALID_INPUT
    assert calls == []


def test_a_failed_play_is_reported_to_certbot(monkeypatch, settings):
    _fleet(
        monkeypatch,
        settings,
        ansible_run(
            host_run("edge-a", failed=1), status=RunStatus.FAILED, return_code=2
        ),
    )

    result = runner.invoke(cli.app, ["cert", "acme-hook", "absent"], env=_CHALLENGE)

    assert result.exit_code == ExitCode.DEPLOYMENT_FAILED
    assert "full output:" in result.output


def test_the_hook_is_hidden_from_help():
    result = runner.invoke(cli.app, ["cert", "--help"])

    assert "acme-hook" not in result.output
