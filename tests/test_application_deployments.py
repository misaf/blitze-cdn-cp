# ruff: noqa: F403,F405
from application_support import *


def _await_terminal(
    repository: Repository, deployment_id: str, timeout: float = 5.0
) -> DeploymentStatus:
    deadline = time.monotonic() + timeout
    pending = {DeploymentStatus.QUEUED, DeploymentStatus.RUNNING}
    while time.monotonic() < deadline:
        status = repository.deployments.get_deployment(deployment_id).status
        if status not in pending:
            return status
        time.sleep(0.01)
    raise AssertionError(f"deployment {deployment_id} never finished")


def _await_workflow(
    repository: Repository, resource_id: str, timeout: float = 5.0
) -> WorkflowStatus:
    """Wait for the workflow covering a queued run to close.

    A separate wait from `_await_terminal`: the deployment reaches a terminal
    status inside the convergence, and the workflow closes around it, so the
    two finish in that order and asserting on the second right after the first
    is a race.
    """
    deadline = time.monotonic() + timeout
    pending = {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    while time.monotonic() < deadline:
        for workflow in repository.workflows.list_workflows(10):
            if workflow.resource_id == resource_id and workflow.status not in pending:
                return workflow.status
        time.sleep(0.01)
    raise AssertionError(f"no workflow for {resource_id} finished")


def test_upload_and_request_certificate_preserve_ssl_mode(
    settings, site_payload, certificate_pair
):
    class FakeIssuer:
        def issue(self, site, email):
            assert site.name == "cdn-example-com"
            assert email == "owner@example.com"
            return certificate_pair()

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        issuer=FakeIssuer(),
        preflight=FakePreflight(),
    )  # type: ignore[arg-type]
    _seed_proxied_record(control)
    certificate, key = certificate_pair()

    uploaded = control.certificates.upload_certificate(
        "cdn-example-com", certificate, key, "alice"
    )
    assert uploaded.source == "uploaded"
    assert repository.sites.get_site("cdn-example-com").certificate_mode == "uploaded"
    assert repository.sites.get_site("cdn-example-com").ssl_mode == "off"

    control.dns.update_record(
        "example.com",
        "cdn",
        RecordType.A,
        RecordPatch(ssl_mode="full"),
        "alice",
    )

    requested = control.certificates.request_certificate(
        "cdn-example-com", "alice", "owner@example.com"
    )
    assert requested.source == "acme"
    assert control.certificates.certificate("cdn-example-com") == requested
    assert repository.sites.get_site("cdn-example-com").certificate_mode == "requested"
    assert repository.sites.get_site("cdn-example-com").ssl_mode == "full"

    result = control.deployments.deploy("alice", check=True)
    assert result.status is DeploymentStatus.SUCCEEDED
    desired = settings.generated_vars_path.read_text(encoding="utf-8")
    assert "certificate_source_path" in desired
    assert "PRIVATE KEY" not in desired
    document = yaml.safe_load(desired)["blitzecdn_nginx_sites"][0]
    fingerprint = requested.fingerprint_sha256
    assert document["certificate_path"].endswith(f"/fullchain-{fingerprint}.pem")
    assert document["certificate_key_path"].endswith(f"/privkey-{fingerprint}.pem")


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
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    _seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"cdn-example-com": expected}
    assert result.skipped == {}
    assert result.deployment is not None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    record = control.dns.get_record("example.com", "cdn", RecordType.A)
    assert record.ssl_mode is expected
    assert record.ssl_automatic_mode is SslAutomaticMode.AUTO
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
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    _seed_automatic_ssl_record(control)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {"cdn-example-com": SslMode.FLEXIBLE}
    assert (
        control.dns.get_record("example.com", "cdn", RecordType.A).ssl_mode
        is SslMode.FLEXIBLE
    )


def test_custom_ssl_mode_is_never_scanned_or_changed(settings):
    runner = FakeRunner()
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    _seed_automatic_ssl_record(control, automatic=SslAutomaticMode.CUSTOM)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.scanned == ()
    assert result.upgraded == {}
    assert runner.origin_checks == []
    assert (
        control.dns.get_record("example.com", "cdn", RecordType.A).ssl_mode
        is SslMode.OFF
    )


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
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository, runner=runner)
    _seed_automatic_ssl_record(control, mode=SslMode.FULL)

    result = control.automatic_ssl.reconcile("scheduler")

    assert result.upgraded == {}
    assert "cdn-example-com" in result.skipped
    assert result.deployment is None
    assert (
        control.dns.get_record("example.com", "cdn", RecordType.A).ssl_mode
        is SslMode.FULL
    )


def test_reconcile_issues_ready_first_certificate_and_deploys(
    settings, certificate_pair
):
    class FakeIssuer:
        def issue(self, site, email):
            assert email == "ops@example.com"
            return certificate_pair((site.server_names[0],))

    configured = settings.model_copy(update={"acme_default_email": "ops@example.com"})
    repository = Repository(configured.database_path)
    control = ControlPlane(
        settings=configured,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a"))]),
        issuer=FakeIssuer(),
        preflight=FakePreflight(),
    )  # type: ignore[arg-type]
    site_name = _seed_proxied_record(control).site_name

    result = control.certificates.reconcile_certificates("timer")

    assert result.issued == (site_name,)
    assert result.skipped == {}
    assert result.failed == {}
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert repository.sites.get_site(site_name).certificate_mode == "requested"
    assert repository.sites.get_site(site_name).ssl_mode == "off"


def test_reconcile_skips_blocked_site_without_contacting_ca(settings, certificate_pair):
    class UnexpectedIssuer:
        def issue(self, _site, _email):
            raise AssertionError("blocked preflight must not contact the CA")

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),
        issuer=UnexpectedIssuer(),
        preflight=FakePreflight(("dns",)),
    )  # type: ignore[arg-type]
    site_name = _seed_proxied_record(control).site_name

    result = control.certificates.reconcile_certificates("timer")

    assert result.issued == ()
    assert "dns" in result.skipped[site_name]
    assert result.deployment is None


def test_request_certificate_requires_email(settings, site_payload):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    from blitzecdn.exceptions import ConflictError

    with pytest.raises(ConflictError, match="email"):
        control.certificates.request_certificate("cdn-example-com", "alice")


def test_certificate_upload_holds_deployment_lock(
    settings, site_payload, certificate_pair
):
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
            from blitzecdn.infrastructure.certificates import CertificateStore

            return CertificateStore(settings).install(
                site, certificate, key, source=source, email=email
            )

    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=LockingRunner(),
        certificate_store=RecordingStore(),  # type: ignore[arg-type]
    )
    _seed_proxied_record(control)
    events.clear()  # seeding does not take the deployment lock
    certificate, key = certificate_pair()

    control.certificates.upload_certificate(
        "cdn-example-com", certificate, key, "alice"
    )

    assert events == ["locked", "installed", "unlocked"]


def test_a_canary_records_its_limit_and_passes_it_to_ansible(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a"))])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    result = control.deployments.deploy("alice", host_limit=" edge-a ")

    assert result.host_limit == "edge-a", "the limit is normalised before storage"
    assert runner.host_limits == ["edge-a"]


def test_a_canary_is_never_the_automatic_rollback_target(settings):
    """A limited run only proves one edge reached that snapshot.

    Rolling the fleet back to it would converge every other edge onto a state
    it had never been given, which is the disagreement rollback exists to end.
    """
    repository = Repository(settings.database_path)
    runner = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]

    # Three distinct desired states, made the only supported way: by changing
    # records. A snapshot carries records and derives sites from them, so
    # editing the derived table would produce three identical snapshots.
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord(domain="example.com", name="cdn", value="198.51.100.10", proxied=True)
    )
    full = control.deployments.deploy("alice")

    repository.zones.delete_record("example.com", "cdn", RecordType.A)
    canary = control.deployments.deploy("alice", host_limit="edge-a")
    assert canary.status is DeploymentStatus.SUCCEEDED

    # A third, distinct state, so both earlier snapshots are eligible and the
    # canary is the more recent of the two. Without the filter it would win.
    repository.zones.create_record(
        DnsRecord(
            domain="example.com", name="other", value="198.51.100.11", proxied=True
        )
    )
    assert (
        repository.deployments.successful_rollback_target(repository.snapshot()).id
        == full.id
    )


def test_a_malformed_limit_is_refused_before_a_deployment_is_recorded(
    settings, site_payload
):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    with pytest.raises(ValueError, match="only narrow a deploy"):
        control.deployments.deploy("alice", host_limit="edge-a:!edge-b")

    assert repository.deployments.list_deployments(5) == []


def _in_sync_run():
    return ansible_run(host_run("edge-a"), host_run("edge-b"))


def _drifted_run():
    """edge-a would rewrite two vhosts and reload; edge-b is converged.

    The task names matter now: a drift report says which configuration moved,
    not only how many tasks would run.
    """
    return ansible_run(
        host_run(
            "edge-a",
            changes=("Render managed sites", "Enable desired sites", "Reload Nginx"),
        ),
        host_run("edge-b"),
    )


def test_drift_check_runs_without_changing_anything(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_in_sync_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.deployments.check_drift("alice")

    assert runner.check_modes == [True], "a drift check must never apply changes"
    assert report.in_sync is True
    assert report.drifted == ()


def test_drift_check_names_the_edges_that_moved(settings, site_payload):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    report = control.deployments.check_drift("alice")

    assert report.in_sync is False
    assert [host.host for host in report.drifted] == ["edge-a"]
    assert any(
        event.action == "drift.checked" and event.details["drifted"] == ["edge-a"]
        for event in repository.audit_log.list_audit_events(10)
    )


def test_a_drift_report_can_be_reread_from_the_recorded_deployment(
    settings, site_payload
):
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    first = control.deployments.check_drift("alice")
    again = control.deployments.drift_report(first.deployment_id)

    assert again.hosts == first.hosts


def test_an_applied_deployment_is_not_a_drift_report(settings, site_payload):
    """Its output says what it did, not what had drifted."""
    repository = Repository(settings.database_path)
    runner = FakeRunner([_drifted_run()])
    control = ControlPlane(settings=settings, repository=repository, runner=runner)  # type: ignore[arg-type]
    repository.sites.create_site(CdnSite.model_validate(site_payload))

    applied = control.deployments.deploy("alice")
    with pytest.raises(ConflictError, match="applied changes"):
        control.deployments.drift_report(applied.id)


class _RecordingIssuer:
    """Stands in for certbot: hands back a fresh pair and remembers the call."""

    def __init__(self, certificate_pair, *, fails: set[str] | None = None) -> None:
        self._pair = certificate_pair
        self._fails = fails or set()
        self.issued: list[tuple[str, str]] = []

    def issue(self, site, email):
        if site.name in self._fails:
            raise ExecutionError("challenge failed")
        self.issued.append((site.name, email))
        return self._pair((site.server_names[0],), days=90)


def _proxied_site_with_certificate(control, repository, certificate_pair, *, days):
    record = _seed_proxied_record(control)
    certificate, key = certificate_pair((record.fqdn,), days=days)
    return control.certificates.upload_certificate(
        record.site_name, certificate, key, "alice"
    )


def test_certificate_statuses_report_time_left(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    _proxied_site_with_certificate(control, repository, certificate_pair, days=10)

    statuses = control.certificates.certificate_statuses()

    assert len(statuses) == 1
    assert statuses[0].days_remaining == 9  # a whole day has not yet elapsed
    assert statuses[0].renewable is False, "an uploaded certificate is not renewable"
    assert control.certificates.expiring_certificates() == statuses


def test_a_healthy_certificate_is_not_reported_as_expiring(settings, certificate_pair):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings, repository=repository, runner=FakeRunner()
    )  # type: ignore[arg-type]
    _proxied_site_with_certificate(control, repository, certificate_pair, days=89)

    assert control.certificates.certificate_statuses() != []
    assert control.certificates.expiring_certificates() == []


def test_renewal_reissues_only_what_is_due(settings, certificate_pair):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.settings = settings

    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label, days in (("due", 5), ("healthy", 80)):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com",
                name=label,
                value="198.51.100.10",
                proxied=True,
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=days)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        # Uploaded certificates are never renewable, so re-request each one to
        # put it under ACME management the way a real ACME site would be.
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )
    issuer.issued.clear()

    # Both now carry the issuer's 90-day certificate, so nothing is due.
    assert control.certificates.renew_certificates("alice").renewed == ()
    assert issuer.issued == []

    assert sorted(
        control.certificates.renew_certificates("alice", force=True).renewed
    ) == [
        "due-example-com",
        "healthy-example-com",
    ]


def test_a_spent_renewal_budget_stops_between_sites_and_says_so(
    settings, certificate_pair, monkeypatch
):
    """A truncated sweep is a slower renewal, not a missed one.

    The budget is checked between sites and never during one, so a request that
    runs out of time cannot leave the CA believing it issued a certificate this
    store never recorded. Whatever it did not reach comes back under `skipped`
    and is picked up by the next run.
    """
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("one", "two"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )
    issuer.issued.clear()

    # Time runs out the moment the first site has been renewed.
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(
        "blitzecdn.application.certificates.monotonic", lambda: next(clock)
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
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("one", "two"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )

    result = control.certificates.renew_certificates("alice", force=True)

    assert len(result.renewed) == 2
    assert result.skipped == ()


def test_an_uploaded_certificate_near_expiry_is_reported_not_renewed(
    settings, certificate_pair
):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _proxied_site_with_certificate(control, repository, certificate_pair, days=3)

    result = control.certificates.renew_certificates("alice")

    assert result.renewed == ()
    assert issuer.issued == [], "BlitzeCDN must not reissue someone else's certificate"
    assert "uploaded, not issued by BlitzeCDN" in result.skipped[0]


def test_one_failing_renewal_does_not_stop_the_others(settings, certificate_pair):
    """A scheduled renewal must make progress even when a site is unreachable."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair, fails={"broken-example-com"})
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )

    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("broken", "fine"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=5)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        # Restamp the stored metadata as an ACME issue registered to an
        # address, which is what a real renewable certificate looks like.
        info = control._certificate_store.get(record.site_name)
        path = settings.certificate_dir / record.site_name / "metadata.json"
        path.write_text(
            info.model_copy(
                update={"source": CertificateSource.ACME, "email": "ops@example.com"}
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )

    result = control.certificates.renew_certificates("alice")

    assert result.renewed == ("fine-example-com",)
    assert len(result.failed) == 1
    assert "broken-example-com" in result.failed[0]


def _two_acme_sites(control, certificate_pair):
    """Two sites under ACME management, both freshly issued and not yet due."""
    control.dns.create_domain(Domain(name="example.com"), "alice")
    for label in ("first", "second"):
        record = control.dns.create_record(
            DnsRecord(
                domain="example.com", name=label, value="198.51.100.10", proxied=True
            ),
            "alice",
        )
        certificate, key = certificate_pair((record.fqdn,), days=80)
        control.certificates.upload_certificate(
            record.site_name, certificate, key, "alice"
        )
        control.certificates.request_certificate(
            record.site_name, "alice", "ops@example.com"
        )


def test_renewal_can_be_narrowed_to_named_sites(settings, certificate_pair):
    """Retrying one failure must not push the others through a rate-limited CA."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    result = control.certificates.renew_certificates(
        "alice", force=True, sites=["first-example-com"]
    )

    assert result.renewed == ("first-example-com",)
    # The unselected site never reached the CA at all.
    assert [site for site, _ in issuer.issued] == ["first-example-com"]


def test_renewal_rejects_a_site_it_has_no_certificate_for(settings, certificate_pair):
    """A typo must not read as 'nothing was due', which is how expiries are missed."""
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)
    issuer.issued.clear()

    with pytest.raises(NotFoundError, match="frist-example-com"):
        control.certificates.renew_certificates(
            "alice", force=True, sites=["frist-example-com"]
        )

    # Nothing was renewed before the unknown name was noticed.
    assert issuer.issued == []


def test_renewal_records_the_selector_in_the_audit_trail(settings, certificate_pair):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=FakePreflight(),  # type: ignore[arg-type]
    )
    _two_acme_sites(control, certificate_pair)

    control.certificates.renew_certificates(
        "alice", force=True, sites=["second-example-com"]
    )
    narrowed = repository.audit_log.list_audit_events()[0]
    control.certificates.renew_certificates("alice")
    full = repository.audit_log.list_audit_events()[0]

    assert narrowed.action == "certificates.renewed"
    assert narrowed.details["sites"] == ["second-example-com"]
    # A full sweep is distinguishable from a narrowed one that renewed nothing.
    assert full.details["sites"] is None
