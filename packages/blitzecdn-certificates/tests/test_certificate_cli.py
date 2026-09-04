"""The commands this capability contributes, driven the way an operator does.

`blitzecdn cert list`, `cert renew` and `cert preflight` exist only while this
distribution is installed — they reach the CLI through this package's
``blitzecdn_cli_commands`` hook — so they belong here for the same reason the
`origin check` cases belong in ``packages/blitzecdn-origins/tests``.

They were in ``tests/entrypoints/test_cli.py``, listed by name in a
``REQUIRES_CERTIFICATES`` set the shared fixtures read in order to skip them
whenever the wheel was detached. Three `deploy` cases came with them: issuance
during a deployment is this capability joining the deployment, and with the
wheel gone there is no behaviour left to assert rather than an assertion that
fails.
"""

from __future__ import annotations

import json

from blitzecdn_certificates.certificates.domain import (
    RenewalResult,
)
from certificate_support import FakePreflight, certificate_cli_control_plane
from click.utils import strip_ansi
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    host_run,
    repository_on,
    seed_site,
    with_capability_settings,
)
from typer.testing import CliRunner

from blitzecdn.cli import main as cli

runner = CliRunner()


def _control(settings, monkeypatch, runner_double=None, preflight=None):
    return certificate_cli_control_plane(
        settings, monkeypatch, runner_double, preflight or FakePreflight()
    )


def _store(settings):
    return repository_on(settings)


def _seed_certificate(control, certificate_pair, *, days):
    """Create the site a certificate has to belong to, and upload one."""
    site = seed_site(control, name="cdn-example-com", record="cdn", operator="cli")
    certificate, key = certificate_pair((site.server_names[0],), days=days)
    control.certificates.upload_certificate(site.name, certificate, key, "cli")
    return site.name


def _renewal(*, renewed=(), skipped=(), failed=()):
    """Stand in for a real renewal run, which would reach a CA over the network."""
    return RenewalResult(
        renewed=tuple(renewed), skipped=tuple(skipped), failed=tuple(failed)
    )


def test_deploy_can_issue_ready_certificates_and_install_them(settings, monkeypatch):
    configured = with_capability_settings(
        settings, acme_default_email="ops@example.com"
    )
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)])
    control = certificate_cli_control_plane(configured, monkeypatch, fake)
    site = seed_site(control)
    report = FakePreflight().check(site, deployed=True, record_ttl=300)
    requested: list[str] = []
    monkeypatch.setattr(
        control.certificates, "certificate_preflight", lambda _name: report
    )
    # Patched below the reconciliation rather than at `request_certificate`,
    # because the CLI no longer drives issuance itself: it runs the same
    # reconciliation the scheduler does, and this is where that reaches the CA.
    monkeypatch.setattr(
        control.certificates,
        "_issue_certificate_locked",
        lambda target, *_args, **_kwargs: requested.append(target.name),
    )
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: configured)

    result = runner.invoke(
        cli.app, ["deploy", "--yes", "--request-certificates", "--json"]
    )

    assert result.exit_code == 2
    assert requested == []
    assert "No such option" in strip_ansi(result.output)


def test_deploy_refuses_certificate_issuance_for_a_canary(settings, monkeypatch):
    _control(settings, monkeypatch)
    result = runner.invoke(
        cli.app,
        ["deploy", "--yes", "--limit", "edge-01", "--request-certificates"],
        color=False,
    )
    assert result.exit_code == 2
    assert "No such option" in strip_ansi(result.output)


def test_deploy_does_not_contact_ca_when_certificate_preflight_blocks(
    settings, monkeypatch
):
    control = certificate_cli_control_plane(
        settings, monkeypatch, FakeRunner([ansible_run(host_run("edge-a"))])
    )
    site = seed_site(control)
    report = FakePreflight(("dns",)).check(site, deployed=True, record_ttl=300)
    monkeypatch.setattr(
        control.certificates, "certificate_preflight", lambda _name: report
    )

    def _unexpected_request(*_args, **_kwargs):
        raise AssertionError("the CA must not be contacted after a blocked preflight")

    monkeypatch.setattr(
        control.certificates, "_issue_certificate_locked", _unexpected_request
    )
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    result = runner.invoke(
        cli.app, ["deploy", "--yes", "--request-certificates", "--json"]
    )

    assert result.exit_code == 2
    assert "No such option" in strip_ansi(result.output)


def test_cert_list_reports_nothing_when_the_store_is_empty(settings, monkeypatch):
    _control(settings, monkeypatch)
    result = runner.invoke(cli.app, ["cert", "list"])
    assert result.exit_code == 0
    assert "No managed certificates." in result.output


def test_cert_list_exits_four_when_a_certificate_has_expired(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch)
    site_name = _seed_certificate(control, certificate_pair, days=1)
    # Backdate the stored record; installing an expired certificate is refused,
    # so the only way to reach this state is for time to have passed.
    path = settings.certificate_dir / site_name / "metadata.json"
    info = control.certificates.persistence.certificates.get(site_name)
    path.write_text(
        info.model_copy(update={"not_after": info.not_before}).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["cert", "list"])

    assert result.exit_code == cli.ExitCode.CONFLICT


def test_doctor_surfaces_certificates_close_to_expiry(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch)
    _seed_certificate(control, certificate_pair, days=7)

    doctor = runner.invoke(cli.app, ["doctor", "--no-resolver"])
    certificates = runner.invoke(cli.app, ["cert", "list"])

    assert "certificate(s) expire within" not in doctor.output
    assert "days_remaining: 6" in certificates.output


def test_cert_renew_points_at_the_deploy_that_installs_the_result(
    settings, monkeypatch
):
    """Renewal only refreshes the controller's store; edges keep the old one."""
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(renewed=["cdn-example-com"]),
    )

    result = runner.invoke(cli.app, ["cert", "renew"])

    assert result.exit_code == 0
    assert "Renewed 1 certificate(s)" in result.output
    assert "blitzecdn deploy" in result.output


def test_cert_renew_passes_the_expiry_window_and_force_through(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seen = {}

    def _capture(operator, *, within_days, force, sites, budget_seconds):
        seen.update(
            operator=operator,
            within_days=within_days,
            force=force,
            sites=sites,
            budget_seconds=budget_seconds,
        )
        return _renewal()

    monkeypatch.setattr(control.certificates, "renew_certificates", _capture)

    result = runner.invoke(cli.app, ["cert", "renew", "--expiring-in", "45", "--force"])

    assert result.exit_code == 0
    assert seen == {
        "operator": "cli",
        "within_days": 45,
        "force": True,
        "sites": None,
        "budget_seconds": None,
    }


def test_cert_renew_reports_skipped_certificates_without_failing(settings, monkeypatch):
    """An uploaded certificate cannot be reissued here, but that is not an error."""
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(skipped=["cdn-example-com: was uploaded"]),
    )

    result = runner.invoke(cli.app, ["cert", "renew"])

    assert result.exit_code == 0
    assert "was uploaded" in result.output
    assert "Renewed" not in result.output


def test_cert_renew_exits_five_when_a_renewal_fails(settings, monkeypatch):
    """Scheduled runs need a non-zero code to alert on, even if others renewed."""
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(
            renewed=["a-example-com"], failed=["b-example-com: CA said no"]
        ),
    )

    result = runner.invoke(cli.app, ["cert", "renew"])

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert "renewal failed: b-example-com: CA said no" in result.output


def test_cert_renew_json_output_carries_all_three_outcomes(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(renewed=["a"], skipped=["b"], failed=["c"]),
    )

    result = runner.invoke(cli.app, ["cert", "renew", "--json"])

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert json.loads(result.stdout) == {
        "renewed": ["a"],
        "skipped": ["b"],
        "failed": ["c"],
    }
    # The prose summary is for humans; --json must stay parseable.
    assert "Renewed" not in result.stdout


def test_cert_renew_can_deploy_successful_renewals(settings, monkeypatch):
    control = _control(
        settings, monkeypatch, FakeRunner([ansible_run(host_run("edge-a"))])
    )
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(renewed=["cdn-example-com"]),
    )

    result = runner.invoke(cli.app, ["cert", "renew", "--deploy", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["renewed"] == ["cdn-example-com"]
    assert payload["deployment"]["status"] == "succeeded"


def test_cert_renew_does_not_deploy_after_a_failed_renewal(settings, monkeypatch):
    control = _control(settings, monkeypatch, FakeRunner())
    monkeypatch.setattr(
        control.certificates,
        "renew_certificates",
        lambda *a, **k: _renewal(failed=["cdn-example-com: CA said no"]),
    )

    result = runner.invoke(cli.app, ["cert", "renew", "--deploy", "--json"])

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert json.loads(result.stdout)["deployment"] is None


def test_cert_renew_site_option_narrows_the_run(settings, monkeypatch):
    """Retrying one failure must not send every subscription back to the CA."""
    control = _control(settings, monkeypatch)
    seen = {}

    def _capture(operator, *, within_days, force, sites, budget_seconds):
        seen.update(sites=sites)
        return _renewal(renewed=list(sites or []))

    monkeypatch.setattr(control.certificates, "renew_certificates", _capture)

    result = runner.invoke(
        cli.app, ["cert", "renew", "--site", "a-example-com", "--site", "b-example-com"]
    )

    assert result.exit_code == 0
    assert seen["sites"] == ["a-example-com", "b-example-com"]


def test_cert_renew_without_the_site_option_considers_everything(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seen = {}

    def _capture(operator, *, within_days, force, sites, budget_seconds):
        seen.update(sites=sites)
        return _renewal()

    monkeypatch.setattr(control.certificates, "renew_certificates", _capture)

    assert runner.invoke(cli.app, ["cert", "renew"]).exit_code == 0
    assert seen["sites"] is None


def test_cert_preflight_reports_each_check_and_exits_zero_when_ready(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch)
    _seed_certificate(control, certificate_pair, days=30)

    result = runner.invoke(cli.app, ["cert", "preflight", "cdn-example-com"])

    assert result.exit_code == 0
    assert "ready for issuance" in result.output


def test_cert_preflight_exits_three_and_names_the_blocking_check(
    settings, monkeypatch, certificate_pair
):
    """The exit code is what a scheduled readiness check alerts on."""
    control = _control(settings, monkeypatch, preflight=FakePreflight(("dns", "caa")))
    _seed_certificate(control, certificate_pair, days=30)

    result = runner.invoke(cli.app, ["cert", "preflight", "cdn-example-com"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "dns" in result.output
    assert "caa" in result.output


def test_cert_preflight_emits_json_for_a_machine_caller(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch, preflight=FakePreflight(("dns",)))
    _seed_certificate(control, certificate_pair, days=30)

    result = runner.invoke(cli.app, ["cert", "preflight", "cdn-example-com", "--json"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    document = json.loads(result.stdout)
    assert document["site"] == "cdn-example-com"
    assert document["checks"][0]["name"] == "dns"
    assert document["checks"][0]["severity"] == "blocking"
