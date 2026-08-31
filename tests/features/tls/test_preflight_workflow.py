# ruff: noqa: F403,F405
from application_support import *

# ----------------------------------------------------------------------
# Certificate preflight enforcement
#
# The checks themselves are tested in test_preflight.py. These are about what
# the application does with a report: refuse, override, or pass through.
# ----------------------------------------------------------------------


def _preflight_control(settings, certificate_pair, failures=()):
    repository = Repository(settings.database_path)
    issuer = _RecordingIssuer(certificate_pair)
    preflight = FakePreflight(failures)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(),  # type: ignore[arg-type]
        issuer=issuer,
        preflight=preflight,  # type: ignore[arg-type]
    )
    return control, repository, issuer, preflight


def test_a_blocked_preflight_refuses_before_reaching_the_ca(settings, certificate_pair):
    """The rate limit is the thing being protected: no CA request at all."""
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError, match="preflight failed"):
        control.certificates.request_certificate(
            "cdn-example-com", "alice", "ops@example.com"
        )

    assert issuer.issued == []


def test_the_refusal_names_the_failed_check_and_the_way_past_it(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair, ("caa",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError) as raised:
        control.certificates.request_certificate(
            "cdn-example-com", "alice", "ops@example.com"
        )

    assert "caa" in str(raised.value)
    assert "skip_preflight" in str(raised.value)


def test_an_override_issues_and_is_audited_as_its_own_event(settings, certificate_pair):
    control, repository, issuer, _ = _preflight_control(
        settings, certificate_pair, ("dns", "deployed")
    )
    _seed_proxied_record(control)

    info = control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com", skip_preflight=True
    )

    assert info.source == "acme"
    assert issuer.issued == [("cdn-example-com", "ops@example.com")]
    overrides = [
        event
        for event in repository.audit_log.list_audit_events()
        if event.action == "certificate.requested.preflight_overridden"
    ]
    assert len(overrides) == 1
    assert {failure["check"] for failure in overrides[0].details["failures"]} == {
        "dns",
        "deployed",
    }


def test_preflight_is_told_the_records_ttl(settings, certificate_pair):
    """The TTL advisory is only possible if the record's own value reaches it."""
    control, _, _, preflight = _preflight_control(settings, certificate_pair)
    control.dns.create_domain(Domain(name="example.com"), "alice")
    control.dns.create_record(
        DnsRecord(
            domain="example.com",
            name="cdn",
            value="198.51.100.10",
            proxied=True,
            ttl=7200,
        ),
        "alice",
    )

    control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com"
    )

    assert preflight.calls[-1] == ("cdn-example-com", False, 7200)


def test_a_blocked_renewal_is_reported_as_failed_not_silently_skipped(
    settings, certificate_pair
):
    """A renewal that cannot validate has to reach the timer's exit code."""
    control, _, issuer, preflight = _preflight_control(settings, certificate_pair)
    _proxied_site_with_certificate(control, None, certificate_pair, days=3)
    control.certificates.request_certificate(
        "cdn-example-com", "alice", "ops@example.com"
    )
    issuer.issued.clear()
    preflight.failures = ("dns",)

    result = control.certificates.renew_certificates("alice", force=True)

    assert result.renewed == ()
    assert len(result.failed) == 1
    assert "preflight failed" in result.failed[0]
    assert issuer.issued == []


def test_a_check_mode_run_does_not_count_as_deployed(settings, certificate_pair):
    """Check mode proved the play parses; no edge is serving the vhost."""
    control, _, _, _ = _preflight_control(settings, certificate_pair)
    _seed_proxied_record(control)

    # Two runs, so the fake runner needs a result for each.
    control._runner.results.append(ansible_run(host_run("edge-a")))

    control.deployments.deploy("alice", check=True)
    assert control.deployments.site_is_deployed("cdn-example-com") is False

    control.deployments.deploy("alice")
    assert control.deployments.site_is_deployed("cdn-example-com") is True


def test_a_site_absent_from_the_last_deployment_is_not_deployed(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair)
    _seed_proxied_record(control)
    control.deployments.deploy("alice")

    assert control.deployments.site_is_deployed("cdn-example-com") is True
    assert control.deployments.site_is_deployed("other-example-com") is False


def test_certificate_preflight_reports_without_contacting_a_ca(
    settings, certificate_pair
):
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    report = control.certificates.certificate_preflight("cdn-example-com")

    assert report.site == "cdn-example-com"
    assert not report.ok
    assert issuer.issued == []


def test_validate_never_writes_the_file_a_deploy_is_converging(settings):
    """Validation takes no lock, so it must not touch shared desired state.

    A deploy in another process owns `generated_vars_path` between writing its
    snapshot there and Ansible reading it. A validate landing in that window
    would converge the fleet to a document the deployment never recorded —
    worst of all under rollback, which would then rewrite canonical records to
    the old zones while the edges received current state.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner()
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    _seed_proxied_record(control)

    settings.generated_vars_path.parent.mkdir(parents=True, exist_ok=True)
    settings.generated_vars_path.write_text("in-flight deploy\n", encoding="utf-8")

    assert control.deployments.validate() == []

    assert settings.generated_vars_path.read_text(encoding="utf-8") == (
        "in-flight deploy\n"
    )
    scratch = fake.validated[0]
    assert scratch != settings.generated_vars_path
    # Rendered for the run and removed with it, rather than accumulating.
    assert not scratch.exists()


def test_overlapping_runs_never_share_a_variables_file(settings, monkeypatch):
    """Purge, stats and decommission all skip the deployment lock.

    They therefore overlap routinely. A fixed filename per playbook made the
    document shared mutable state: the second writer won, so a two-URL purge
    could find `purge_all: true` in the file by the time its own playbook read
    it and empty the cache on every edge.
    """
    from blitzecdn.core import ansible

    settings.cache_purge_playbook_path.write_text(
        "- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8"
    )
    runner = ansible.AnsibleRunner(settings, FakeEdgeStore())
    seen: list[dict[str, object]] = []

    def capture(*, variables, **_kwargs):
        seen.append(yaml.safe_load(variables.read_text(encoding="utf-8")))
        return _purge_run()

    monkeypatch.setattr(runner._executor, "execute", capture)

    runner.run_cache_purge(
        entries=[PurgeEntry(host="cdn.example.com", uri="/a.js", scheme="http")],
        purge_all=False,
    )
    runner.run_cache_purge(entries=[], purge_all=True)

    assert seen[0]["blitzecdn_cache_purge_all"] is False
    assert seen[1]["blitzecdn_cache_purge_all"] is True
    # Nothing is left behind for a later run to pick up.
    assert list((settings.state_dir / "runs").iterdir()) == []


def test_a_collection_reads_only_its_own_run(settings):
    """Two overlapping collections cannot answer each other's questions.

    They used to share one controller-side directory that each emptied before
    collecting, so a run could wipe the other's reports and read the whole fleet
    as silent. Counters now arrive attached to the run that asked for them, so
    the failure has no way to occur — this pins that property rather than the
    directory bookkeeping that used to approximate it.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 1}])
            ),
            ansible_run(
                _reporting("edge-a", [{"site": "a", "outcome": "HIT", "requests": 99}]),
                _reporting("edge-b", [{"site": "a", "outcome": "MISS", "requests": 5}]),
            ),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]

    first = control.cache.cache_stats("alice")
    second = control.cache.cache_stats("alice")

    assert first.requests == 1
    assert [edge.host for edge in first.reporting] == ["edge-a"]
    assert second.requests == 104
    assert [edge.host for edge in second.reporting] == ["edge-a", "edge-b"]
    # Nothing on the controller's filesystem is involved any more.
    assert not (settings.state_dir / "stats").exists()
