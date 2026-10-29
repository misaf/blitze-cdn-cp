from types import SimpleNamespace

from blitzecdn.capabilities.tls.feature import CERTIFICATES
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import certificates_operations
from blitzecdn.core.features import CapabilityConfig


def test_metadata_provides_required_certificates_capability() -> None:
    metadata = CERTIFICATES.metadata

    assert metadata.name == "certificates"
    assert metadata.capabilities == frozenset({"certificates"})


def test_jobs_are_owned_by_the_certificate_capability() -> None:
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
    config = CertificateConfig.from_capability_config(
        CapabilityConfig(
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

    operations = certificates_operations(
        certificates=certificates,
        automatic_ssl=automatic_ssl,
        config=config,
        deployment_timeout_seconds=900,
    )
    jobs = {job.name: job for job in operations.scheduled_jobs}

    assert set(jobs) == {
        "automatic-ssl-scan",
        "certificate-reconciliation",
        "certificate-renewal",
    }
    assert jobs["certificate-renewal"].lease_seconds == 1200
    jobs["certificate-reconciliation"].run("scheduler")
    assert calls == ["reconcile scheduler"]
    issued.append("example-com")
    jobs["certificate-reconciliation"].run("scheduler")
    jobs["certificate-renewal"].run("scheduler")
    jobs["automatic-ssl-scan"].run("scheduler")
    assert calls[1:] == [
        "reconcile scheduler",
        "scan scheduler",
        "renew scheduler 300",
        "scan scheduler",
    ]
