from types import SimpleNamespace

from blitzecdn import acme_hook
from blitzecdn.infrastructure.ansible import CommandResult


def test_acme_hook_validates_and_runs(monkeypatch, settings):
    calls = []

    class Runner:
        def __init__(self, _settings):
            pass

        def run_acme_challenge(self, **values):
            calls.append(values)
            return CommandResult(0, "ok", "")

    monkeypatch.setattr(acme_hook.Settings, "from_environment", lambda: settings)
    monkeypatch.setattr(acme_hook, "AnsibleRunner", Runner)
    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "present"])
    monkeypatch.setattr(
        acme_hook,
        "os",
        SimpleNamespace(
            environ={
                "CERTBOT_DOMAIN": "cdn.example.com",
                "CERTBOT_TOKEN": "safe_token",
                "CERTBOT_VALIDATION": "safe.validation",
            }
        ),
    )
    assert acme_hook.main() == 0
    assert calls[0]["action"] == "present"


def test_acme_hook_fails_closed(monkeypatch):
    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "bad"])
    assert acme_hook.main() == 1

    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "present"])
    monkeypatch.setenv("CERTBOT_DOMAIN", "bad/domain")
    monkeypatch.setenv("CERTBOT_TOKEN", "bad token")
    assert acme_hook.main() == 1
