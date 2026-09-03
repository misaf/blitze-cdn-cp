from types import SimpleNamespace

from blitzecdn_certificates import plugin

from blitzecdn.core.plugins import CapabilityConfig


def test_metadata_provides_optional_certificates_capability() -> None:
    metadata = plugin.blitzecdn_plugin_metadata()

    assert metadata.name == "certificates"
    assert metadata.capabilities == frozenset({"certificates"})
    assert not metadata.required


def test_jobs_are_owned_by_the_certificate_package(monkeypatch) -> None:
    calls: list[str] = []
    issued: list[str] = []
    certificates = SimpleNamespace(
        reconcile_certificates=lambda operator: (
            calls.append(f"reconcile {operator}")
            or SimpleNamespace(issued=tuple(issued))
        ),
        renew_certificates=lambda operator, *, budget_seconds: calls.append(
            f"renew {operator} {budget_seconds}"
        ),
    )
    automatic_ssl = SimpleNamespace(
        reconcile=lambda operator: calls.append(f"scan {operator}")
    )
    monkeypatch.setattr(plugin, "build_certificate_service", lambda _p: certificates)
    monkeypatch.setattr(plugin, "build_automatic_ssl_service", lambda _p: automatic_ssl)

    class Platform:
        """Weak-referenceable, which `SimpleNamespace` is not.

        `certificate_config` caches per control plane in a `WeakKeyDictionary`
        for the same reason the services do, so the double has to be a kind of
        object a weak reference can be taken to.
        """

    platform = Platform()
    platform.settings = SimpleNamespace(deployment_timeout_seconds=900)
    platform.capability_config = SimpleNamespace(
        for_plugin=lambda _name: CapabilityConfig(
            "certificates",
            {},
            {
                "BLITZE_CERTBOT": "certbot",
                "BLITZE_ACME_DEFAULT_EMAIL": "",
                "BLITZE_ACME_CA_DOMAIN": "letsencrypt.org",
                "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS": 3600,
                "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS": 7200,
                "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS": 300,
                "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS": 86_400,
            },
        )
    )

    jobs = {job.name: job for job in plugin.blitzecdn_scheduled_jobs(platform)}

    assert set(jobs) == {
        "automatic-ssl-scan",
        "certificate-reconciliation",
        "certificate-renewal",
    }
    jobs["certificate-reconciliation"].run("scheduler")
    assert calls == ["reconcile scheduler"]
    issued.append("cdn-example-com")
    jobs["certificate-reconciliation"].run("scheduler")
    jobs["certificate-renewal"].run("scheduler")
    jobs["automatic-ssl-scan"].run("scheduler")
    assert calls[1:] == [
        "reconcile scheduler",
        "scan scheduler",
        "renew scheduler 300",
        "scan scheduler",
    ]
