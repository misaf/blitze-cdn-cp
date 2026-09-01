from types import SimpleNamespace

from blitzecdn_certificates import plugin


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
    platform = SimpleNamespace(
        settings=SimpleNamespace(
            certificate_reconcile_interval_seconds=3600,
            certificate_renewal_interval_seconds=7200,
            certificate_renewal_budget_seconds=300,
            ssl_automatic_scan_interval_seconds=86_400,
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
