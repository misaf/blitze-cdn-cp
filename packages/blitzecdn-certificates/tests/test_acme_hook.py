from types import SimpleNamespace

from blitzecdn_certificates import acme_hook
from control_plane_fixtures import ansible_run, host_run


def test_acme_hook_validates_and_runs(monkeypatch, settings):
    calls = []

    def close():
        calls.append({"repository": "closed"})

    class Fleet:
        def run_playbook(self, **values):
            calls.append(values)
            return ansible_run(host_run("edge-a"))

    control = SimpleNamespace(settings=settings, fleet=Fleet(), close=close)
    monkeypatch.setattr(acme_hook.common, "control_plane", lambda: control)
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
    assert calls[0]["name"] == "acme-challenge"
    assert calls[0]["variables"]["blitzecdn_acme_action"] == "present"
    assert calls[1] == {"repository": "closed"}


def test_acme_hook_fails_closed(monkeypatch):
    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "bad"])
    assert acme_hook.main() == 1

    monkeypatch.setattr(acme_hook.sys, "argv", ["acme-hook", "present"])
    monkeypatch.setenv("CERTBOT_DOMAIN", "bad/domain")
    monkeypatch.setenv("CERTBOT_TOKEN", "bad token")
    assert acme_hook.main() == 1
