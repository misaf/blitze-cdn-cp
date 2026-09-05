"""Certificate work seen from the control plane, not from a route or a command.

These four were in ``tests/entrypoints/test_control_plane.py``, named in a
``REQUIRES_CERTIFICATES`` set so the shared fixtures would skip them when this
wheel was detached. They also relied on the ``attach_certificate_test_services``
monkeypatch to make ``control.certificates`` exist at all — a shape production
core never has. Here they build the services through
``blitzecdn_certificates.composition``, which is the path the plugin itself
takes, so the wiring under test is the wiring that ships.
"""

from __future__ import annotations

import pytest
from blitzecdn_certificates.certificates.domain import CertificateSource
from certificate_support import FakePreflight, certificate_control_plane
from control_plane_fixtures import (
    FakeRunner,
    repository_on,
    seed_site,
    with_capability_settings,
)

from blitzecdn.capabilities.tls.policy import CertificateMode
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.core.exceptions import DeploymentBusyError


def test_validate_rejects_acme_on_a_reserved_domain(settings):
    """No public CA issues for .test, so catch it before certbot is invoked."""
    control = certificate_control_plane(settings)
    seed_site(
        control,
        name="api-vendra-test",
        domain="vendra.test",
        record="api",
        certificate_mode=CertificateMode.REQUESTED,
        certificate_path="/etc/blitzecdn/tls/api-vendra-test/fullchain.pem",
        certificate_key_path="/etc/blitzecdn/tls/api-vendra-test/privkey.pem",
    )
    assert any("reserved name" in error for error in control.deployments.validate())


def test_busy_external_work_does_not_create_a_false_workflow(settings):
    """A workflow starts only after this process owns the external-work lock.

    Otherwise an API restart can see the journal entry, fail to acquire the
    lock held by the real worker, and report the refused attempt as interrupted
    work even though that attempt never touched an edge or a CA.
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
            "cdn-example-com", "alice", email="ops@example.com"
        )

    assert repository.workflows.list_workflows(10) == []


def test_a_renewal_blocked_by_a_deployment_is_skipped_not_failed(
    settings, certificate_pair, monkeypatch
):
    """Lock contention is "come back later", not "the CA refused".

    Issuance takes the deployment lock, so a fleet deploy can span a whole
    sweep. Filed under `failed` it read exactly like a CA rejection, which is
    the one thing a renewal report must not get wrong near an expiry — and the
    next run picks the site up regardless.
    """
    control = certificate_control_plane(settings)
    repository = repository_on(settings)
    seed_site(control)
    site = repository.sites.list_sites()[0]
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

    That is a rate-limited issuance spent on nothing, and it is the state worth
    recognising before retrying — so the journal has to distinguish it from an
    interruption after the PEM was stored, which the next issuance corrects for
    free. Both used to arrive as one undifferentiated NEEDS_REVIEW.
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
        control.certificates.request_certificate("cdn-example-com", "alice")

    workflow = repository.workflows.list_workflows(10)[0]
    assert workflow.status is WorkflowStatus.FAILED
    # It got past the CA and no further, which is the whole distinction.
    assert [step.name for step in workflow.steps] == ["issued"]
