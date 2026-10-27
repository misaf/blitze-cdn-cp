"""Issuance, upload, renewal and the Automatic SSL scan, at the service level.

Built through ``certificate_control_plane``, which builds the composition
root's services and substitutes only the named seam.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
import yaml
from support.application import _seed_proxied_record
from support.certificates import (
    FakePreflight,
    _automatic_origin_report,
    _proxied_site_with_certificate,
    _RecordingIssuer,
    _seed_automatic_ssl_record,
    certificate_control_plane,
)
from support.control_plane import (
    FakeRunner,
    ansible_run,
    host_run,
    seed_site,
    with_capability_settings,
)

from blitzecdn.capabilities.deployments.domain import (
    DeploymentRequirementKind,
    DeploymentStatus,
)
from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy
from blitzecdn.capabilities.tls.certificates.domain import CertificateSource
from blitzecdn.capabilities.tls.contract import SslAutomaticMode, SslMode
from blitzecdn.capabilities.workflows.domain import WorkflowStatus
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import NotFoundError


def _two_acme_sites(control, certificate_pair):
    """Two sites under ACME management, both freshly issued and not yet due."""
    for label in ("first", "second"):
        site = seed_site(control, name=f"{label}-example-com", record=label)
        certificate, key = certificate_pair((site.server_names[0],), days=80)
        control.certificates.upload_certificate(site.name, certificate, key, "alice")
        control.certificates.request_certificate(site.name, "alice", "ops@example.com")


def test_issuance_is_outside_activation_transaction_and_failure_keeps_evidence(
    settings, certificate_pair, monkeypatch
):
    class Issuer:
        def issue(self, site, email):
            assert control.transactions.database.current_session() is None
            other = Repository(settings.database_path)
            try:
                (workflow,) = other.workflows.list_workflows()
                assert workflow.status is WorkflowStatus.RUNNING
            finally:
                other.close()
            return certificate_pair()

    control = certificate_control_plane(
        settings, runner=FakeRunner(), issuer=Issuer(), preflight=FakePreflight()
    )
    _seed_proxied_record(control)
    original = control.releases.inputs().digest
    record = control.certificates.events.record

    def refuse_activation_event(event):
        if event.action == "certificate.requested":
            raise RuntimeError("audit unavailable")
        return record(event)

    monkeypatch.setattr(control.certificates.events, "record", refuse_activation_event)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        control.certificates.request_certificate(
            "example-com", "alice", "owner@example.com"
        )

    assert control.releases.inputs().digest == original
    assert not control.transactions.deployment_requirements.pending(
        DeploymentRequirementKind.CERTIFICATES
    )
    # Files and the CA effect cannot be rolled back with canonical activation.
    assert control.certificates.certificate("example-com").source == "acme"
    (workflow,) = control.transactions.workflows.list_workflows()
    assert workflow.status is WorkflowStatus.FAILED
    assert [step.name for step in workflow.steps] == ["issued", "stored"]


def test_upload_and_request_certificate_preserve_ssl_mode(settings, certificate_pair):
    class FakeIssuer:
        def issue(self, site, email):
            assert site.name == "example-com"
            assert email == "owner@example.com"
            return certificate_pair()

    control = certificate_control_plane(
        settings, runner=FakeRunner(), issuer=FakeIssuer(), preflight=FakePreflight()
    )
    _seed_proxied_record(control)
    certificate, key = certificate_pair()

    uploaded = control.certificates.upload_certificate(
        "example-com", certificate, key, "alice"
    )
    assert uploaded.source == "uploaded"
    assert (
        control.sites.get_site("example-com").policy.tls.certificate_mode == "uploaded"
    )
    assert control.sites.get_site("example-com").policy.tls.ssl_mode == "off"

    control.dns.update_domain(
        "example.com", PatchPolicy.from_patch(DomainPatch(ssl_mode="full")), "alice"
    )

    requested = control.certificates.request_certificate(
        "example-com", "alice", "owner@example.com"
    )
    assert requested.source == "acme"
    assert control.certificates.certificate("example-com") == requested
    assert (
        control.sites.get_site("example-com").policy.tls.certificate_mode == "requested"
    )
    assert control.sites.get_site("example-com").policy.tls.ssl_mode == "full"

    result = control.deployments.deploy("alice", check=True)
    assert result.status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "certificate_source_path" in desired
    assert "PRIVATE KEY" not in desired
    document = yaml.safe_load(desired)["blitzecdn_nginx_sites"][0]
    assert document["certificate_path"].endswith(
        f"/fullchain-{requested.certificate_digest}.pem"
    )
    assert document["certificate_key_path"].endswith(
        f"/privkey-{requested.private_key_digest}.pem"
    )


def test_new_certificate_material_owes_the_fleet_a_deployment(
    settings, certificate_pair
):
    """The requirement lifecycle, from the service that raises it to the one that
    clears it.

    Storing a certificate does not put it on an edge. The gap is recorded
    durably, so a crash cannot lose it, and a check-mode run must not clear it.
    """
    repository = Repository(settings.database_path)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(
            [ansible_run(host_run("edge-a")), ansible_run(host_run("edge-a"))]
        ),
    )
    _seed_proxied_record(control)
    certificate, key = certificate_pair()
    kind = DeploymentRequirementKind.CERTIFICATES

    assert not repository.deployment_requirements.pending(kind)

    control.certificates.upload_certificate("example-com", certificate, key, "alice")
    assert repository.deployment_requirements.pending(kind)

    checked = control.deployments.deploy("alice", check=True)
    assert checked.status is DeploymentStatus.SUCCEEDED
    assert repository.deployment_requirements.pending(kind)

    converged = control.deployments.deploy("alice")
    assert converged.status is DeploymentStatus.SUCCEEDED
    assert not repository.deployment_requirements.pending(kind)


def test_a_canary_leaves_the_certificate_requirement_owed(settings, certificate_pair):
    """A green canary converged only the named edges, so the rest are still owed."""
    control = certificate_control_plane(settings, runner=FakeRunner())
    _seed_proxied_record(control)
    certificate, key = certificate_pair()
    kind = DeploymentRequirementKind.CERTIFICATES
    requirements = control.transactions.deployment_requirements

    control.certificates.upload_certificate("example-com", certificate, key, "alice")
    canary = control.deployments.deploy("alice", host_limit="edge-a")
    assert canary.status is DeploymentStatus.SUCCEEDED
    assert requirements.pending(kind)

    assert control.deployments.deploy("alice").status is DeploymentStatus.SUCCEEDED
    assert not requirements.pending(kind)


def test_a_rollback_leaves_the_certificate_requirement_owed(settings, certificate_pair):
    """A rollback restores a release that may predate the newest material."""
    control = certificate_control_plane(settings, runner=FakeRunner())
    _seed_proxied_record(control)
    kind = DeploymentRequirementKind.CERTIFICATES
    requirements = control.transactions.deployment_requirements

    certificate, key = certificate_pair(days=40)
    control.certificates.upload_certificate("example-com", certificate, key, "alice")
    deployed = control.deployments.deploy("alice")
    assert not requirements.pending(kind)

    certificate, key = certificate_pair(days=80)
    control.certificates.upload_certificate("example-com", certificate, key, "alice")
    restored = control.deployments.rollback("alice", deployed.id)
    assert restored.status is DeploymentStatus.SUCCEEDED
    assert requirements.pending(kind)


def test_a_deployment_queued_before_the_material_does_not_satisfy_it(
    settings, certificate_pair
):
    """Its release was compiled before the certificate, so it cannot clear the
    requirement, and convergence queues a fresh fleet deployment rather than
    reuse it."""
    control = certificate_control_plane(settings, runner=FakeRunner())
    _seed_proxied_record(control)
    kind = DeploymentRequirementKind.CERTIFICATES
    requirements = control.transactions.deployment_requirements

    stale = control.deployments.submit_deployment("alice")
    certificate, key = certificate_pair()
    control.certificates.upload_certificate("example-com", certificate, key, "alice")

    fresh = control.deployments.converge_requirements("alice")
    assert fresh is not None
    assert fresh.id != stale.id
    assert control.deployments.converge_requirements("alice").id == fresh.id

    stale_run = control.deployments.converge(stale, "alice")
    assert stale_run.status is DeploymentStatus.SUCCEEDED
    assert requirements.pending(kind)

    fresh_run = control.deployments.converge(fresh, "alice")
    assert fresh_run.status is DeploymentStatus.SUCCEEDED
    assert not requirements.pending(kind)


@pytest.mark.parametrize(
    ("tls_verified", "expected"),
    [(True, SslMode.FULL_STRICT), (False, SslMode.FULL)],
)
def test_automatic_ssl_upgrades_to_the_strongest_fleet_verified_mode(
    settings, tls_verified, expected
):
    runner = FakeRunner(
        [
            ansible_run(_automatic_origin_report(SslMode.OFF)),
            ansible_run(
                _automatic_origin_report(
                    SslMode.FULL_STRICT,
                    tls_verified=tls_verified,
                )
            ),
            ansible_run(host_run("edge-a")),
        ]
    )
    repository = Repository(settings.database_path)
    control = certificate_control_plane(settings, runner=runner)
    _seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"example-com": expected}
    assert result.skipped == {}
    assert result.deployment is not None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    site = control.sites.get_site("example-com")
    assert site.policy.tls.ssl_mode is expected
    assert site.policy.tls.ssl_automatic_mode is SslAutomaticMode.AUTO
    event = next(
        event
        for event in repository.audit_log.list_audit_events(20)
        if event.action == "ssl.automatic.upgraded"
    )
    assert event.action == "ssl.automatic.upgraded"
    assert event.details["to"] == expected.value


def test_automatic_ssl_uses_flexible_when_only_http_is_healthy(settings):
    runner = FakeRunner(
        [
            ansible_run(_automatic_origin_report(SslMode.OFF)),
            ansible_run(
                _automatic_origin_report(
                    SslMode.FULL_STRICT,
                    reachable=False,
                )
            ),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = certificate_control_plane(settings, runner=runner)
    _seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"example-com": SslMode.FLEXIBLE}
    assert control.sites.get_site("example-com").policy.tls.ssl_mode is SslMode.FLEXIBLE


def test_custom_ssl_mode_is_never_scanned_or_changed(settings):
    runner = FakeRunner()
    control = certificate_control_plane(settings, runner=runner)
    _seed_automatic_ssl_record(control, automatic=SslAutomaticMode.CUSTOM)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.scanned == ()
    assert result.upgraded == {}
    assert runner.playbooks == []
    assert control.sites.get_site("example-com").policy.tls.ssl_mode is SslMode.OFF


def test_automatic_ssl_never_downgrades_when_strict_is_unavailable(settings):
    runner = FakeRunner(
        [
            ansible_run(
                _automatic_origin_report(
                    SslMode.FULL,
                    reachable=True,
                )
            ),
            ansible_run(
                _automatic_origin_report(
                    SslMode.FULL_STRICT,
                    reachable=False,
                )
            ),
        ]
    )
    control = certificate_control_plane(settings, runner=runner)
    _seed_automatic_ssl_record(control, mode=SslMode.FULL)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {}
    assert "example-com" in result.skipped
    assert result.deployment is None
    assert control.sites.get_site("example-com").policy.tls.ssl_mode is SslMode.FULL


def test_reconcile_issues_ready_first_certificate_and_queues_its_deployment(
    settings, certificate_pair
):
    class FakeIssuer:
        def issue(self, site, email):
            assert email == "ops@example.com"
            return certificate_pair((site.server_names[0],))

    configured = with_capability_settings(
        settings, acme_default_email="ops@example.com"
    )
    control = certificate_control_plane(
        configured,
        runner=FakeRunner([ansible_run(host_run("edge-a"))]),
        issuer=FakeIssuer(),
        preflight=FakePreflight(),
    )
    site_name = _seed_proxied_record(control).name

    result = control.certificates.reconcile_certificates("timer")

    assert result.issued == (site_name,)
    assert result.skipped == {}
    assert result.failed == {}
    assert result.deployment.status is DeploymentStatus.QUEUED
    # A scheduled run asks again after the job; it gets the same deployment.
    assert control.deployments.converge_requirements("timer").id == (
        result.deployment.id
    )
    assert control.sites.get_site(site_name).policy.tls.certificate_mode == "requested"
    assert control.sites.get_site(site_name).policy.tls.ssl_mode == "off"


def test_reconcile_skips_blocked_site_without_contacting_ca(settings, certificate_pair):
    class UnexpectedIssuer:
        def issue(self, _site, _email):
            raise AssertionError("blocked preflight must not contact the CA")

    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=UnexpectedIssuer(),
        preflight=FakePreflight(("dns",)),
    )
    site_name = _seed_proxied_record(control).name

    result = control.certificates.reconcile_certificates("timer")

    assert result.issued == ()
    assert "dns" in result.skipped[site_name]
    assert result.deployment is None


def test_request_certificate_requires_email(settings):
    control = certificate_control_plane(settings, runner=FakeRunner())
    seed_site(control)
    from blitzecdn.core.exceptions import ConflictError

    with pytest.raises(ConflictError, match="email"):
        control.certificates.request_certificate("example-com", "alice")


def test_certificate_upload_holds_deployment_lock(settings, certificate_pair):
    events: list[str] = []

    class LockingRunner(FakeRunner):
        @contextmanager
        def lock(self):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

    class RecordingStore:
        def install(self, site, certificate, key, *, source, email=None):
            assert events == ["locked"]
            events.append("installed")
            from importlib import import_module

            certificate_store_class = import_module(
                "blitzecdn.capabilities.tls.certificates.adapters"
            ).CertificateStore

            return certificate_store_class(settings).install(
                site, certificate, key, source=source, email=email
            )

    control = certificate_control_plane(
        settings,
        runner=LockingRunner(),
        certificate_store=RecordingStore(),
    )
    _seed_proxied_record(control)
    events.clear()  # seeding does not take the deployment lock
    certificate, key = certificate_pair()

    control.certificates.upload_certificate("example-com", certificate, key, "alice")

    assert events == ["locked", "installed", "unlocked"]


def test_certificate_statuses_report_time_left(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = certificate_control_plane(settings, runner=FakeRunner())
    _proxied_site_with_certificate(control, repository, certificate_pair, days=10)

    statuses = control.certificates.certificate_statuses()

    assert len(statuses) == 1
    assert statuses[0].days_remaining == 9  # a whole day has not yet elapsed
    assert statuses[0].renewable is False, "an uploaded certificate is not renewable"
    assert control.certificates.expiring_certificates() == statuses


def test_a_healthy_certificate_is_not_reported_as_expiring(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = certificate_control_plane(settings, runner=FakeRunner())
    _proxied_site_with_certificate(control, repository, certificate_pair, days=89)

    assert control.certificates.certificate_statuses() != []
    assert control.certificates.expiring_certificates() == []


def test_renewal_reissues_only_what_is_due(settings, certificate_pair):
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )

    for label, days in (("due", 5), ("healthy", 80)):
        site = seed_site(control, name=f"{label}-example-com", record=label)
        certificate, key = certificate_pair((site.server_names[0],), days=days)
        control.certificates.upload_certificate(site.name, certificate, key, "alice")
        # Uploaded certificates are never renewable, so re-request each one to
        # put it under ACME management the way a real ACME site would be.
        control.certificates.request_certificate(site.name, "alice", "ops@example.com")
    issuer.issued.clear()

    # Both now carry the issuer's 90-day certificate, so nothing is due.
    assert control.certificates.renew_certificates("alice").renewed == ()
    assert issuer.issued == []

    assert sorted(
        control.certificates.renew_certificates("alice", force=True).renewed
    ) == [
        "example-com--due",
        "example-com--healthy",
    ]


def test_a_spent_renewal_budget_stops_between_sites_and_says_so(
    settings, certificate_pair, monkeypatch
):
    """A truncated sweep is a slower renewal, not a missed one.

    The budget is checked between sites, never during one, so the CA cannot have
    issued something unrecorded. What was not reached comes back as `skipped`.
    """
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    for label in ("one", "two"):
        site = seed_site(control, name=f"{label}-example-com", record=label)
        certificate, key = certificate_pair((site.server_names[0],), days=5)
        control.certificates.upload_certificate(site.name, certificate, key, "alice")
        control.certificates.request_certificate(site.name, "alice", "ops@example.com")
    issuer.issued.clear()

    # Time runs out the moment the first site has been renewed.
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(
        "blitzecdn.capabilities.tls.certificates.service.issuance.monotonic",
        lambda: next(clock),
    )

    result = control.certificates.renew_certificates(
        "alice", force=True, budget_seconds=1
    )

    assert len(result.renewed) == 1
    assert len(issuer.issued) == 1, "the budget must not interrupt an issuance"
    assert len(result.skipped) == 1
    assert "renewal budget" in result.skipped[0]
    assert "retried by the next run" in result.skipped[0]


def test_renewal_without_a_budget_is_unbounded(settings, certificate_pair):
    """The CLI and the timer pass no budget, and must keep sweeping everything."""
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    for label in ("one", "two"):
        site = seed_site(control, name=f"{label}-example-com", record=label)
        certificate, key = certificate_pair((site.server_names[0],), days=5)
        control.certificates.upload_certificate(site.name, certificate, key, "alice")
        control.certificates.request_certificate(site.name, "alice", "ops@example.com")

    result = control.certificates.renew_certificates("alice", force=True)

    assert len(result.renewed) == 2
    assert result.skipped == ()


def test_an_uploaded_certificate_near_expiry_is_reported_not_renewed(
    settings, certificate_pair
):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    _proxied_site_with_certificate(control, repository, certificate_pair, days=3)

    result = control.certificates.renew_certificates("alice")

    assert result.renewed == ()
    assert issuer.issued == [], "BlitzeCDN must not reissue someone else's certificate"
    assert "uploaded, not issued by BlitzeCDN" in result.skipped[0]


def test_a_renewal_that_returned_the_same_certificate_is_not_reported_renewed(
    settings, certificate_pair
):
    """The failure mode certbot's `--keep-until-expiring` default produces.

    The issuer returns the certificate already in place and every signal except
    the fingerprint says the renewal worked, which is how a fleet reaches expiry
    with a timer reporting success.
    """

    class _StuckIssuer:
        """Reissues the same bytes forever, the way a kept lineage does."""

        def __init__(self, pair):
            self._pair = pair
            self._first: tuple[bytes, bytes] | None = None

        def issue(self, site, email):
            if self._first is None:
                self._first = self._pair((site.server_names[0],), days=90)
            return self._first

    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=_StuckIssuer(certificate_pair),
        preflight=FakePreflight(),
    )
    site = seed_site(control, name="stuck-example-com", record="stuck")
    control.certificates.request_certificate(site.name, "alice", "ops@example.com")

    result = control.certificates.renew_certificates("alice", force=True)

    assert result.renewed == ()
    assert len(result.failed) == 1
    assert "the issuer returned the certificate already in place" in result.failed[0]


def test_one_failing_renewal_does_not_stop_the_others(settings, certificate_pair):
    """A scheduled renewal must make progress even when a site is unreachable."""
    issuer = _RecordingIssuer(certificate_pair, fails={"example-com--broken"})
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )

    for label in ("broken", "fine"):
        site = seed_site(control, name=f"{label}-example-com", record=label)
        certificate, key = certificate_pair((site.server_names[0],), days=5)
        control.certificates.upload_certificate(site.name, certificate, key, "alice")
        # Restamp the stored metadata as an ACME issue registered to an
        # address, which is what a real renewable certificate looks like.
        info = control.certificates.persistence.certificates.get(site.name)
        path = settings.certificate_dir / site.name / "metadata.json"
        path.write_text(
            info.model_copy(
                update={"source": CertificateSource.ACME, "email": "ops@example.com"}
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )

    result = control.certificates.renew_certificates("alice")

    assert result.renewed == ("example-com--fine",)
    assert len(result.failed) == 1
    assert "example-com--broken" in result.failed[0]


def test_renewal_can_be_narrowed_to_named_sites(settings, certificate_pair):
    """Retrying one failure must not push the others through a rate-limited CA."""
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    result = control.certificates.renew_certificates(
        "alice", force=True, sites=["example-com--first"]
    )

    assert result.renewed == ("example-com--first",)
    # The unselected site never reached the CA at all.
    assert [site for site, _ in issuer.issued] == ["example-com--first"]


def test_renewal_rejects_a_site_it_has_no_certificate_for(settings, certificate_pair):
    """A typo must not read as 'nothing was due', which is how expiries are missed."""
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    with pytest.raises(NotFoundError, match="example-com--frist"):
        control.certificates.renew_certificates(
            "alice", force=True, sites=["example-com--frist"]
        )

    # Nothing was renewed before the unknown name was noticed.
    assert issuer.issued == []


def test_renewal_records_the_selector_in_the_audit_trail(settings, certificate_pair):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = certificate_control_plane(
        settings,
        runner=FakeRunner(),
        issuer=issuer,
        preflight=FakePreflight(),
    )
    _two_acme_sites(control, certificate_pair)

    control.certificates.renew_certificates(
        "alice", force=True, sites=["example-com--second"]
    )
    narrowed = repository.audit_log.list_audit_events()[0]
    control.certificates.renew_certificates("alice")
    full = repository.audit_log.list_audit_events()[0]

    assert narrowed.action == "certificates.renewed"
    assert narrowed.details["sites"] == ["example-com--second"]
    # A full sweep is distinguishable from a narrowed one that renewed nothing.
    assert full.details["sites"] is None
