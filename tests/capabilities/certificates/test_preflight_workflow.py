import pytest
import yaml
from support.application import _seed_proxied_record
from support.certificates import (
    FakePreflight,
    _proxied_site_with_certificate,
    _RecordingIssuer,
    certificate_control_plane,
)
from support.control_plane import (
    FakeEdgeStore,
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    seed_site,
)

from blitzecdn.capabilities.edges.adapters.roster import EdgeRoster
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError

# ----------------------------------------------------------------------
# Certificate preflight enforcement: what the application does with a report
# (refuse, override, pass through). The checks are in test_preflight.py.
# ----------------------------------------------------------------------


def _preflight_control(settings, certificate_pair, failures=()):
    """The control plane these cases share, with the CA and the checks faked."""
    issuer = _RecordingIssuer(certificate_pair)
    preflight = FakePreflight(failures)
    control = certificate_control_plane(
        settings, runner=FakeRunner(), issuer=issuer, preflight=preflight
    )
    return control, Repository(settings.database_path), issuer, preflight


def test_a_blocked_preflight_refuses_before_reaching_the_ca(settings, certificate_pair):
    """The rate limit is the thing being protected: no CA request at all."""
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError, match="preflight failed"):
        control.certificates.request_certificate(
            "example-com", "alice", "ops@example.com"
        )

    assert issuer.issued == []


def test_the_refusal_names_the_failed_check_and_the_way_past_it(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair, ("caa",))
    _seed_proxied_record(control)

    with pytest.raises(ConflictError) as raised:
        control.certificates.request_certificate(
            "example-com", "alice", "ops@example.com"
        )

    assert "caa" in str(raised.value)
    assert "skip_preflight" in str(raised.value)


def test_an_override_issues_and_is_audited_as_its_own_event(settings, certificate_pair):
    control, repository, issuer, _ = _preflight_control(
        settings, certificate_pair, ("dns", "deployed")
    )
    _seed_proxied_record(control)

    info = control.certificates.request_certificate(
        "example-com", "alice", "ops@example.com", skip_preflight=True
    )

    assert info.source == "acme"
    assert issuer.issued == [("example-com", "ops@example.com")]
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
    """The TTL is the record's, not the site's, so it has to come from `dns`."""
    control, _, _, preflight = _preflight_control(settings, certificate_pair)
    seed_site(control, name="example-com", record="cdn", ttl=7200)

    control.certificates.request_certificate("example-com", "alice", "ops@example.com")

    assert preflight.calls[-1] == ("example-com", False, 7200)


def test_a_blocked_renewal_is_reported_as_failed_not_silently_skipped(
    settings, certificate_pair
):
    """A renewal that cannot validate has to reach the timer's exit code."""
    control, _, issuer, preflight = _preflight_control(settings, certificate_pair)
    _proxied_site_with_certificate(control, None, certificate_pair, days=3)
    control.certificates.request_certificate("example-com", "alice", "ops@example.com")
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
    control.infrastructure.runner.results.append(ansible_run(host_run("edge-a")))

    control.deployments.deploy("alice", check=True)
    assert control.deployments.site_is_deployed("example-com") is False

    control.deployments.deploy("alice")
    assert control.deployments.site_is_deployed("example-com") is True


def test_a_site_absent_from_the_last_deployment_is_not_deployed(
    settings, certificate_pair
):
    control, _, _, _ = _preflight_control(settings, certificate_pair)
    _seed_proxied_record(control)
    control.deployments.deploy("alice")

    assert control.deployments.site_is_deployed("example-com") is True
    assert control.deployments.site_is_deployed("example-com--other") is False


def test_certificate_preflight_reports_without_contacting_a_ca(
    settings, certificate_pair
):
    control, _, issuer, _ = _preflight_control(settings, certificate_pair, ("dns",))
    _seed_proxied_record(control)

    report = control.certificates.certificate_preflight("example-com")

    assert report.site == "example-com"
    assert not report.ok
    assert issuer.issued == []


def test_validate_never_writes_the_file_a_deploy_is_converging(settings):
    """Validation takes no lock, so it must not touch shared desired state.

    A deploy in another process owns `generated_vars_path` between writing it and
    Ansible reading it.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner()
    control = build_application(settings=settings, repository=repository, runner=fake)
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
    """Purge, stats, origin check and decommission all skip the deployment lock.

    They overlap routinely, so a shared variables file would let one run read
    another's document (a two-URL purge finding `purge_all: true`). Asserted on
    `run_playbook` itself, with the same play and different variables.
    """
    from blitzecdn.core import ansible

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    playbook = settings.state_dir / "operation.yml"
    playbook.write_text("- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8")
    runner = ansible.AnsibleRunner(settings, EdgeRoster(FakeEdgeStore()))
    seen: list[dict[str, object]] = []

    def capture(request):
        seen.append(yaml.safe_load(request.variables.read_text(encoding="utf-8")))
        return ansible_run(host_run("edge-a", changed=1))

    monkeypatch.setattr(runner._executor, "execute", capture)

    runner.run_playbook(
        name="operation", playbook=playbook, variables={"blitzecdn_scope": "one"}
    )
    runner.run_playbook(
        name="operation", playbook=playbook, variables={"blitzecdn_scope": "every"}
    )

    assert seen[0]["blitzecdn_scope"] == "one"
    assert seen[1]["blitzecdn_scope"] == "every"
    # Nothing is left behind for a later run to pick up.
    assert list((settings.state_dir / "runs").iterdir()) == []
