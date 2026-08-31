from types import SimpleNamespace

from conftest import ansible_run, host_run

from blitzecdn import acme_hook


def test_acme_hook_validates_and_runs(monkeypatch, settings):
    calls = []

    original_close = acme_hook.Repository.close

    def close(repository):
        calls.append({"repository": "closed"})
        original_close(repository)

    class Runner:
        def __init__(self, _settings, edges):
            # The hook opens the same database the inventory plugin will read,
            # so the runner it builds is handed a real edge store. Recorded
            # rather than ignored: passing the wrong one would mean a challenge
            # published to a different fleet than the CA is about to validate.
            calls.append({"edges": type(edges).__name__})

        def run_acme_challenge(self, **values):
            calls.append(values)
            return ansible_run(host_run("edge-a"))

    monkeypatch.setattr(acme_hook.Settings, "from_environment", lambda: settings)
    monkeypatch.setattr(acme_hook, "AnsibleRunner", Runner)
    monkeypatch.setattr(acme_hook.Repository, "close", close)
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
    assert calls[0] == {"edges": "EdgeStore"}
    assert calls[1]["action"] == "present"
    assert calls[2] == {"repository": "closed"}


def test_acme_hook_fails_closed(monkeypatch):
    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "bad"])
    assert acme_hook.main() == 1

    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "present"])
    monkeypatch.setenv("CERTBOT_DOMAIN", "bad/domain")
    monkeypatch.setenv("CERTBOT_TOKEN", "bad token")
    assert acme_hook.main() == 1
