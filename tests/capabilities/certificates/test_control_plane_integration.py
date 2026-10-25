"""Certificate work seen from the control plane, not from a route or a command.

The services are built through ``blitzecdn.capabilities.tls.wiring``, the
path the feature itself takes.
"""

from __future__ import annotations

import pytest
from support.certificates import FakePreflight, certificate_control_plane
from support.control_plane import (
    FakeRunner,
    repository_on,
    seed_site,
    with_capability_settings,
)

from blitzecdn.capabilities.tls.certificates.domain import CertificateSource
from blitzecdn.capabilities.tls.contract import CertificateMode
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.core.exceptions import DeploymentBusyError


def test_validate_rejects_acme_on_a_reserved_domain(settings):
    """No public CA issues for .test, so catch it before certbot is invoked."""
    control = certificate_control_plane(settings)
    # Managed paths name the host a certificate was issued for, and a derived host
    # is named after its zone.
    seed_site(
        control,
        name="vendra-test",
        domain="vendra.test",
        record="api",
        certificate_mode=CertificateMode.REQUESTED,
        certificate_path="/etc/blitzecdn/tls/vendra-test/fullchain.pem",
        certificate_key_path="/etc/blitzecdn/tls/vendra-test/privkey.pem",
    )
    assert any("reserved name" in error for error in control.deployments.validate())


def test_busy_external_work_does_not_create_a_false_workflow(settings):
    """A workflow starts only after this process owns the external-work lock.

    Otherwise a refused attempt could be reported as interrupted work it never
    did.
    """

    class BusyRunner(FakeRunner):
        def lock(self):
            raise DeploymentBusyError("another deployment is already running")

    control = certificate_control_plane(settings, runner=BusyRunner())
    repository = repository_on(settings)

    with pytest.raises(DeploymentBusyError):
        control.deployments.deploy("alice")
    with pytest.raises(DeploymentBusyError):
        control.certificates.request_certificate(
            "example-com", "alice", email="ops@example.com"
        )

    assert repository.workflows.list_workflows(10) == []


def test_a_renewal_blocked_by_a_deployment_is_skipped_not_failed(
    settings, certificate_pair, monkeypatch
):
    """Lock contention is "come back later", not "the CA refused".

    Issuance takes the deployment lock, so a deploy can span a sweep; filing that
    under `failed` would read like a CA rejection.
    """
    control = certificate_control_plane(settings)
    seed_site(control)
    site = control.sites.list_sites()[0]
    certificate, key = certificate_pair((site.server_names[0],), days=5)
    control.certificates.persistence.certificates.install(
        site, certificate, key, source=CertificateSource.ACME, email="ops@example.com"
    )

    def busy(*_args, **_kwargs):
        raise DeploymentBusyError("another deployment is already running")

    monkeypatch.setattr(control.certificates, "request_certificate", busy)

    result = control.certificates.renew_certificates("alice")

    assert result.failed == ()
    assert len(result.skipped) == 1
    assert "a deployment was running" in result.skipped[0]
    assert result.ok is True


def test_an_interrupted_issuance_says_how_far_it_got(settings, monkeypatch):
    """The CA may have issued a certificate that reached no disk here.

    That spent a rate-limited issuance, so the journal distinguishes it from an
    interruption after the PEM was stored.
    """
    configured = with_capability_settings(
        settings, acme_default_email="ops@example.com"
    )

    class DyingStore:
        def install(self, *_args, **_kwargs):
            raise OSError("disk full")

    control = certificate_control_plane(
        configured, certificate_store=DyingStore(), preflight=FakePreflight()
    )
    repository = repository_on(configured)
    seed_site(control)
    monkeypatch.setattr(
        control.certificates.execution.issuer,
        "issue",
        lambda *_a, **_k: (b"cert", b"key"),
    )

    with pytest.raises(OSError):
        control.certificates.request_certificate("example-com", "alice")

    workflow = repository.workflows.list_workflows(10)[0]
    assert workflow.status is WorkflowStatus.FAILED
    # It got past the CA and no further, which is the whole distinction.
    assert [step.name for step in workflow.steps] == ["issued"]
