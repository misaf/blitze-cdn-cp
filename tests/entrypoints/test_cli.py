import json
import sys
from importlib import import_module
from importlib.util import find_spec

import pytest
from click.utils import strip_ansi
from control_plane_fixtures import (
    FakePreflight,
    FakeRunner,
    ansible_run,
    cli_control_plane,
    host_run,
    repository_on,
    seed_site,
)
from typer.testing import CliRunner

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.cli import main as cli
from blitzecdn.core.database import Repository
from blitzecdn.core.plugins import PluginRejection
from blitzecdn.core.runs import RunStatus
from blitzecdn.features.diagnostics import cli as diagnostics_cli
from blitzecdn.features.dns.domain import DnsRecord, Domain

certificate_domain = (
    import_module("blitzecdn_certificates.certificates.domain")
    if find_spec("blitzecdn_certificates") is not None
    else None
)
PreflightCheck = getattr(certificate_domain, "PreflightCheck", None)
PreflightSeverity = getattr(certificate_domain, "PreflightSeverity", None)
RenewalResult = getattr(certificate_domain, "RenewalResult", None)

REQUIRES_CERTIFICATES = frozenset(
    {
        "test_cert_list_reports_nothing_when_the_store_is_empty",
        "test_cert_list_exits_four_when_a_certificate_has_expired",
        "test_doctor_surfaces_certificates_close_to_expiry",
        "test_cert_renew_points_at_the_deploy_that_installs_the_result",
        "test_cert_renew_passes_the_expiry_window_and_force_through",
        "test_cert_renew_reports_skipped_certificates_without_failing",
        "test_cert_renew_exits_five_when_a_renewal_fails",
        "test_cert_renew_json_output_carries_all_three_outcomes",
        "test_cert_renew_can_deploy_successful_renewals",
        "test_cert_renew_does_not_deploy_after_a_failed_renewal",
        "test_cert_renew_site_option_narrows_the_run",
        "test_cert_renew_without_the_site_option_considers_everything",
        "test_cert_preflight_reports_each_check_and_exits_zero_when_ready",
        "test_cert_preflight_exits_three_and_names_the_blocking_check",
        "test_cert_preflight_emits_json_for_a_machine_caller",
        "test_deploy_can_issue_ready_certificates_and_install_them",
        "test_deploy_refuses_certificate_issuance_for_a_canary",
        "test_deploy_does_not_contact_ca_when_certificate_preflight_blocks",
    }
)

runner = CliRunner()


def _seed_site(control, label="api", origin="198.51.100.20", name=None):
    """A site and the record that routes one hostname to it, through the CLI.

    Two commands where there used to be one flag. `record add --proxied` did
    both jobs because a record *was* a site; they are separate objects now, so
    the fixture creates the site and then points a hostname at it.
    """
    site = name or f"{label}-example-com"
    assert (
        runner.invoke(cli.app, ["site", "create", site, "--origin", origin]).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli.app, ["record", "add", "example.com", label, "--site", site]
        ).exit_code
        == 0
    )
    return site


def test_cli_domain_record_status_audit_and_doctor(settings, monkeypatch, tmp_path):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    assert runner.invoke(cli.app, ["domain", "add", "example.com"]).exit_code == 0
    _seed_site(control, "cdn", "198.51.100.10")
    listed = runner.invoke(cli.app, ["site", "list", "--json"])
    assert listed.exit_code == 0 and "cdn-example-com" in listed.stdout
    assert runner.invoke(cli.app, ["doctor", "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["audit", "--json"]).exit_code == 0
    assert (
        runner.invoke(
            cli.app, ["record", "remove", "example.com", "cdn", "--yes"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(cli.app, ["domain", "remove", "example.com", "--yes"]).exit_code
        == 0
    )


def test_setup_and_edge_workflow(tmp_path, monkeypatch):
    """Register, list, update and remove an edge — with no inventory file.

    `setup` no longer creates one. The fleet is a table, and Ansible reads it
    through the `blitzecdn` inventory plugin, so the roster these commands
    change *is* the inventory rather than something that has to be written out
    to become one.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 0
    assert not (tmp_path / "ansible/inventory/hosts.yml").exists()
    # The inventory plugin refuses a database that does not exist, so a fresh
    # install where `setup` left none could not run a single playbook — every
    # one of them failed to parse its inventory before reaching a task.
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    settings = cli.Settings.from_environment({}, project_dir=tmp_path)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    added = runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
            "--public-address",
            "203.0.113.10",
            "--json",
        ],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["name"] == "edge-01"
    listed = runner.invoke(cli.app, ["edge", "list", "--json"])
    assert json.loads(listed.stdout)[0]["host"] == "192.0.2.10"
    updated = runner.invoke(
        cli.app,
        [
            "edge",
            "update",
            "edge-01",
            "--public-address",
            "203.0.113.11",
            "--json",
        ],
    )
    assert json.loads(updated.stdout)["public_addresses"] == ["203.0.113.11"]
    # --no-decommission because `setup` scaffolds no playbooks: this test is
    # about the roster, and the teardown path is covered below.
    removed = runner.invoke(
        cli.app, ["edge", "remove", "edge-01", "--yes", "--no-decommission"]
    )
    assert removed.exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []


def test_schema_only_setup_initializes_database_without_scaffolding(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["setup", "--schema-only"])

    assert result.exit_code == 0
    assert cli.Settings.from_environment(
        {}, project_dir=tmp_path
    ).database_path.exists()
    assert result.stdout == ""


def test_registering_an_edge_is_audited(tmp_path, monkeypatch):
    """Who added this host, and when.

    Unanswerable before: the CLI held the inventory file and rewrote it
    directly, so nothing about the fleet ever reached the audit trail.
    """
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli.app, ["setup"]).exit_code == 0
    settings = cli.Settings.from_environment({}, project_dir=tmp_path)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)

    runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "10.0.0.0/8",
        ],
    )
    runner.invoke(cli.app, ["edge", "update", "edge-01", "--port", "7845"])

    events = Repository(settings.database_path).audit_log.list_audit_events()
    actions = [event.action for event in events]
    assert "edge.added" in actions
    assert "edge.updated" in actions
    update = next(event for event in events if event.action == "edge.updated")
    assert update.details["fields"] == ["port"]


def test_run_reports_domain_errors_without_a_traceback(settings, monkeypatch, capsys):
    """`run()` is outside Click, so it must exit rather than raise."""
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "record", "list", "absent.example"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    # NOT_FOUND rather than INVALID_INPUT: the API answers 404 here, and a
    # caller driving the CLI needs the same distinction. Everything used to
    # exit 2, so "no such zone" and "bad flag" were the same answer.
    assert exit_info.value.code == cli.ExitCode.NOT_FOUND
    assert "does not exist" in capsys.readouterr().err


def test_cli_route_and_unroute_move_a_hostname_on_and_off_the_edge(
    settings, monkeypatch
):
    """`record route` / `record unroute` is the CDN switch for one hostname.

    The site outlives the switch. `record proxy --off` used to take the whole
    virtual host away, policy included, because the record carried it; here it
    takes the hostname off a site that stays configured.
    """
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app, ["site", "create", "api-example-com", "--origin", "198.51.100.20"]
    )
    added = runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20", "--json"],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["site"] is None
    # Unrouted, the site answers for nothing.
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [[]]

    routed = runner.invoke(
        cli.app,
        [
            "record",
            "route",
            "example.com",
            "api",
            "--site",
            "api-example-com",
            "--json",
        ],
    )
    assert routed.exit_code == 0
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [["api.example.com"]]
    assert sites[0]["origin_host"] == "198.51.100.20"

    runner.invoke(
        cli.app,
        ["record", "unroute", "example.com", "api", "--value", "203.0.113.7"],
    )
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [[]]

    # And `record add` refuses to be both at once.
    assert (
        runner.invoke(
            cli.app,
            [
                "record",
                "add",
                "example.com",
                "www",
                "--value",
                "198.51.100.21",
                "--site",
                "api-example-com",
            ],
        ).exit_code
        != 0
    )


def test_cli_always_use_https_toggle_drives_the_derived_site(settings, monkeypatch):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")

    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert sites[0]["always_use_https"] is False

    enabled = runner.invoke(
        cli.app,
        ["site", "always-use-https", "api-example-com", "--on", "--json"],
    )

    assert enabled.exit_code == 0
    assert json.loads(enabled.stdout)["always_use_https"] is True
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert sites[0]["always_use_https"] is True

    disabled = runner.invoke(
        cli.app,
        ["site", "always-use-https", "api-example-com", "--off"],
    )
    assert disabled.exit_code == 0
    assert "now disabled" in disabled.stdout
    assert control.sites.get_site("api-example-com").always_use_https is False


def test_cli_firewall_replaces_only_the_lists_it_names(settings, monkeypatch):
    """Merge semantics, and the derived site carries the result.

    The CLI keeps the lists the operator did not mention, unlike the API PATCH
    that replaces the whole block. Getting this wrong would silently drop the
    rules a second invocation did not repeat.
    """
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")

    first = runner.invoke(
        cli.app,
        [
            "site",
            "firewall",
            "api-example-com",
            "--deny-source",
            "203.0.113.0/24",
            "--deny-path",
            "/admin",
            "--json",
        ],
    )
    assert first.exit_code == 0

    # Names only the country list; the source and path rules must survive.
    second = runner.invoke(
        cli.app,
        ["site", "firewall", "api-example-com", "--deny-country", "ru", "--json"],
    )
    assert second.exit_code == 0
    firewall = json.loads(second.stdout)["firewall"]
    assert firewall["deny_sources"] == ["203.0.113.0/24"]
    assert firewall["denied_paths"] == ["/admin"]
    assert firewall["denied_countries"] == ["RU"]

    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert sites[0]["firewall"]["denied_countries"] == ["RU"]

    cleared = runner.invoke(
        cli.app, ["site", "firewall", "api-example-com", "--clear", "--json"]
    )
    assert json.loads(cleared.stdout)["firewall"]["deny_sources"] == []


def test_cli_firewall_refuses_a_network_with_host_bits_set(settings, monkeypatch):
    """203.0.113.5/24 means one address to the operator and 256 to nginx."""
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20"],
    )
    result = runner.invoke(
        cli.app,
        [
            "site",
            "firewall",
            "api-example-com",
            "--deny-source",
            "203.0.113.5/24",
        ],
    )
    assert result.exit_code != 0


def test_cli_firewall_requires_a_rule_or_clear(settings, monkeypatch):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app, ["record", "add", "example.com", "api", "--value", "198.51.100.20"]
    )
    bare = runner.invoke(cli.app, ["site", "firewall", "api-example-com"])
    assert bare.exit_code != 0
    conflicting = runner.invoke(
        cli.app,
        [
            "site",
            "firewall",
            "api-example-com",
            "--clear",
            "--deny-path",
            "/admin",
        ],
    )
    assert conflicting.exit_code != 0


def test_cli_dns_export_hides_addresses_for_proxied_records(settings, monkeypatch):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")
    exported = json.loads(runner.invoke(cli.app, ["dns", "export", "--json"]).stdout)
    assert exported[0]["proxied"] is True
    assert exported[0]["site"] == "api-example-com"
    assert "value" not in exported[0]


def test_cli_plan_deploy_status_and_rollback(settings, monkeypatch):
    repository = Repository(settings.database_path)
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control)
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    planned = runner.invoke(cli.app, ["plan", "--json"])
    assert planned.exit_code == 0
    deployed = runner.invoke(cli.app, ["deploy", "--yes", "--json"])
    assert deployed.exit_code == 0
    deployment_id = json.loads(deployed.stdout)["id"]
    assert runner.invoke(cli.app, ["status", deployment_id, "--json"]).exit_code == 0
    assert runner.invoke(cli.app, ["status", "--json"]).exit_code == 0
    assert (
        runner.invoke(cli.app, ["rollback", deployment_id, "--yes", "--json"]).exit_code
        == 0
    )


def test_deploy_can_issue_ready_certificates_and_install_them(settings, monkeypatch):
    repository = Repository(settings.database_path)
    configured = settings.model_copy(update={"acme_default_email": "ops@example.com"})
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)])
    control = ControlPlane(settings=configured, repository=repository, runner=fake)  # type: ignore[arg-type]
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
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(host_run("edge-a"))]),
    )  # type: ignore[arg-type]
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


def test_interactive_deploy_validates_previews_and_applies(settings, monkeypatch):
    repository = Repository(settings.database_path)
    # `validate` reads the first result without consuming it, so one entry
    # serves both it and the preview; the second is the apply.
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
        ]
    )
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    # Nothing optional: this is about the interactive flow, and a site left on
    # its defaults asks for `cache` and `compression`, which the core-only
    # workspace does not have and would refuse the deploy over.
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    result = runner.invoke(cli.app, ["deploy"], input="y\n")
    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    # The preview names the task that would change. It used to echo Ansible's
    # own output verbatim, which meant several hundred lines to find this in.
    assert "edge-a: would change 1 task(s)" in result.stdout
    assert "Render managed sites" in result.stdout
    assert "succeeded" in result.stdout


def _control(settings, monkeypatch, runner_double=None, preflight=None):
    return cli_control_plane(settings, monkeypatch, runner_double, preflight)


def _store(settings):
    return repository_on(settings)


def _seed_certificate(control, certificate_pair, *, days):
    """Create the site a certificate has to belong to, and upload one."""
    site = seed_site(control, name="cdn-example-com", record="cdn", operator="cli")
    certificate, key = certificate_pair((site.server_names[0],), days=days)
    control.certificates.upload_certificate(site.name, certificate, key, "cli")
    return site.name


def _in_sync():
    return ansible_run(host_run("edge-a", ok=9))


def _drifted():
    return ansible_run(
        host_run("edge-a", ok=9, changes=("Render managed sites", "Reload Nginx"))
    )


def test_drift_exits_zero_when_the_fleet_matches(settings, monkeypatch):
    _control(settings, monkeypatch, FakeRunner([_in_sync()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == 0
    assert "All 1 edges match desired state." in result.output


def test_drift_exits_six_when_an_edge_has_moved(settings, monkeypatch):
    """A dedicated code so a scheduled check can tell drift from a broken check."""
    _control(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift"])
    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "edge-a would change 2 task(s)" in result.output


def test_drift_json_output_is_machine_readable(settings, monkeypatch):
    _control(settings, monkeypatch, FakeRunner([_drifted()]))
    result = runner.invoke(cli.app, ["drift", "--json"])
    payload = json.loads(result.output)
    assert payload["in_sync"] is False
    assert payload["hosts"][0]["changed"] == 2


def test_deploy_with_a_limit_says_the_rollout_is_unfinished(settings, monkeypatch):
    """Leaving a canary half-applied silently is the failure worth preventing."""
    control = _control(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(host_run("edge-a")) for _ in range(3)]),
    )
    settings.inventory_path.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts:\n"
        "        edge-a:\n          ansible_host: 198.51.100.1\n",
        encoding="utf-8",
    )
    control.dns.create_domain(Domain(name="example.com"), "cli")

    result = runner.invoke(cli.app, ["deploy", "--yes", "--limit", "edge-a"])

    assert result.exit_code == 0
    assert "This was a canary against 'edge-a'" in result.output
    assert "without --limit to finish the rollout" in result.output


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
    info = control._certificate_store.get(site_name)
    path.write_text(
        info.model_copy(update={"not_after": info.not_before}).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["cert", "list"])

    assert result.exit_code == cli.ExitCode.CONFLICT


# The three `origin check` cases that were here moved to
# `packages/blitzecdn-origins/tests/` with the command. They belong there twice
# over: the command exists only while that distribution is installed, and a
# core-only run would fail on a CLI surface core no longer contributes.


def test_doctor_surfaces_certificates_close_to_expiry(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch)
    _seed_certificate(control, certificate_pair, days=7)

    doctor = runner.invoke(cli.app, ["doctor", "--no-resolver"])
    certificates = runner.invoke(cli.app, ["cert", "list"])

    assert "certificate(s) expire within" not in doctor.output
    assert "days_remaining: 6" in certificates.output


def _renewal(*, renewed=(), skipped=(), failed=()):
    """Stand in for a real renewal run, which would reach a CA over the network."""
    return RenewalResult(
        renewed=tuple(renewed), skipped=tuple(skipped), failed=tuple(failed)
    )


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


def test_control_plane_factory_builds_from_the_environment(settings, monkeypatch):
    """The two factories are the only wiring between Settings and the commands."""
    monkeypatch.setattr(
        cli.Settings, "from_environment", classmethod(lambda cls: settings)
    )
    assert cli.common.settings() is settings
    assert cli.common.control_plane().settings is settings


def test_validate_exits_three_and_lists_what_is_wrong(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.deployments, "validate", lambda: ["playbook is missing"]
    )

    result = runner.invoke(cli.app, ["validate"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "playbook is missing" in result.output


def test_plan_exits_five_when_check_mode_fails(settings, monkeypatch):
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    host_run("edge-a", ok=0, unreachable=1),
                    status=RunStatus.FAILED,
                    return_code=2,
                )
            ]
        ),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    assert runner.invoke(cli.app, ["plan"]).exit_code == cli.ExitCode.DEPLOYMENT_FAILED


def test_interactive_deploy_refuses_to_preview_an_invalid_configuration(
    settings, monkeypatch
):
    """The preview costs an Ansible run, so validation gates it."""
    control = _control(settings, monkeypatch)
    monkeypatch.setattr(
        control.deployments, "validate", lambda: ["inventory has no edges"]
    )

    result = runner.invoke(cli.app, ["deploy"], input="y\n")

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "inventory has no edges" in result.output
    assert "Previewing changes" not in result.output


def test_interactive_deploy_applies_nothing_when_the_operator_declines(
    settings, monkeypatch
):
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control, cache_enabled=False, compression="off")
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    result = runner.invoke(cli.app, ["deploy"], input="n\n")

    assert result.exit_code == 1
    # The preview ran in check mode; the apply never did.
    assert fake.check_modes == [True]


def test_rollback_changes_nothing_when_the_operator_declines(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    called = []
    monkeypatch.setattr(
        control.deployments, "rollback", lambda *a, **k: called.append(a)
    )

    result = runner.invoke(cli.app, ["rollback", "some-deployment-id"], input="n\n")

    assert result.exit_code == 1
    assert called == []


def test_drift_says_so_when_no_edge_answered(settings, monkeypatch):
    """An empty recap is silence, not agreement, so it must not read as in-sync."""
    _control(settings, monkeypatch, FakeRunner([ansible_run()]))

    result = runner.invoke(cli.app, ["drift"])

    assert result.exit_code == cli.ExitCode.DRIFT_DETECTED
    assert "No edge reported a result" in result.output


def test_domain_remove_keeps_the_zone_when_the_operator_declines(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "cli")

    result = runner.invoke(cli.app, ["domain", "remove", "example.com"], input="n\n")

    assert result.exit_code == 1
    assert [domain.name for domain in control.dns.list_domains()] == ["example.com"]


def test_record_remove_keeps_the_record_when_the_operator_declines(
    settings, monkeypatch
):
    control = _control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "cli")
    control.dns.create_record(
        DnsRecord(domain="example.com", name="cdn", value="198.51.100.10"), "cli"
    )

    result = runner.invoke(
        cli.app, ["record", "remove", "example.com", "cdn"], input="n\n"
    )

    assert result.exit_code == 1
    assert len(_store(settings).zones.list_records("example.com")) == 1


def _torn_down():
    return ansible_run(host_run("edge-01", ok=12, changed=7))


def _unreachable():
    return ansible_run(
        host_run("edge-01", ok=0, unreachable=1),
        status=RunStatus.FAILED,
        return_code=4,
    )


def _add_edge():
    return runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
        ],
    )


def test_edge_remove_tears_the_host_down_before_forgetting_it(settings, monkeypatch):
    """The teardown has to reach the host while it is still in inventory."""
    double = FakeRunner([_torn_down()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code == 0
    assert double.decommissions == ["edge-01"]
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []


def test_edge_remove_keeps_the_entry_when_the_teardown_fails(settings, monkeypatch):
    """An unreachable host keeps its entry: its private keys are still on it."""
    double = FakeRunner([_unreachable()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes"])

    assert result.exit_code != 0
    listed = json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout)
    assert [edge["name"] for edge in listed] == ["edge-01"]


def test_edge_remove_force_drops_a_host_that_no_longer_exists(settings, monkeypatch):
    """--force is for a destroyed instance, which can never report clean."""
    double = FakeRunner([_unreachable()])
    _control(settings, monkeypatch, double)
    _add_edge()

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01", "--yes", "--force"])

    assert result.exit_code == 0
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout) == []
    actions = [event.action for event in _store(settings).audit_log.list_audit_events()]
    assert "edge.decommission_failed" in actions


def test_edge_remove_keeps_the_edge_when_the_operator_declines(settings, monkeypatch):
    _control(settings, monkeypatch)
    runner.invoke(
        cli.app,
        [
            "edge",
            "add",
            "edge-01",
            "--host",
            "192.0.2.10",
            "--ssh-source",
            "198.51.100.8/24",
        ],
    )

    result = runner.invoke(cli.app, ["edge", "remove", "edge-01"], input="n\n")

    assert result.exit_code == 1
    assert json.loads(runner.invoke(cli.app, ["edge", "list", "--json"]).stdout)


def test_edge_add_refuses_to_open_ssh_to_the_world(settings, monkeypatch):
    """The firewall fails closed; an edge with no management CIDR cannot exist."""
    _control(settings, monkeypatch)

    result = runner.invoke(
        cli.app,
        ["edge", "add", "edge-01", "--host", "192.0.2.10"],
        color=False,
    )

    assert result.exit_code != 0
    assert "ssh-source" in strip_ansi(result.output)


def test_serve_refuses_to_start_unauthenticated(settings, monkeypatch):
    unauthenticated = settings.model_copy(update={"api_keys": {}})
    monkeypatch.setattr(cli.common, "settings", lambda: unauthenticated)
    started = []
    monkeypatch.setattr(
        diagnostics_cli.uvicorn, "run", lambda *a, **k: started.append(a)
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code != 0
    assert started == []


def test_site_show_reveals_defaults_the_create_never_mentioned(settings, monkeypatch):
    """A site is derived, so its resolved policy is not visible on the record."""
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["site", "show", "cdn-example-com", "--json"])

    assert result.exit_code == 0
    site = json.loads(result.stdout)
    assert site["server_names"] == ["cdn.example.com"]
    assert site["origin_host"] == "198.51.100.10"
    # Never set on the record; only the derived site shows them.
    assert site["ssl_mode"] == "off"
    assert site["ssl_automatic_mode"] == "auto"
    assert site["minimum_tls_version"] == "1.2"
    assert site["http3_enabled"] is False
    assert site["cache_query_string_mode"] == "include"
    assert "origin_scheme" not in site
    assert site["cache_valid_success"] == "10m"


def test_site_show_reports_an_unknown_site_without_a_traceback(settings, monkeypatch):
    _control(settings, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "site", "show", "absent"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.NOT_FOUND


def test_site_ssl_changes_the_combined_mode(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(
        control,
        name="cdn-example-com",
        record="cdn",
        operator="cli",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/edge.pem",
        certificate_key_path="/etc/ssl/private/edge.key",
    )

    result = runner.invoke(
        cli.app,
        ["site", "ssl", "cdn-example-com", "--mode", "full_strict"],
    )

    assert result.exit_code == 0
    assert control.sites.get_site("cdn-example-com").ssl_mode == "full_strict"
    assert "Run 'blitzecdn deploy'" in result.stdout


def test_site_http3_toggles_quic_for_a_tls_site(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(
        control,
        name="cdn-example-com",
        record="cdn",
        operator="cli",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/edge.pem",
        certificate_key_path="/etc/ssl/private/edge.key",
    )

    result = runner.invoke(
        cli.app, ["site", "http3", "cdn-example-com", "--on", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["http3_enabled"] is True
    assert control.sites.get_site("cdn-example-com").http3_enabled is True


def test_site_under_attack_toggles_edge_mitigation(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app, ["site", "under-attack", "cdn-example-com", "--on", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["under_attack_mode"] is True
    assert control.sites.get_site("cdn-example-com").under_attack_mode is True


def test_site_ssl_automatic_can_opt_out_to_custom(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app,
        [
            "site",
            "ssl-automatic",
            "cdn-example-com",
            "--mode",
            "custom",
        ],
    )

    assert result.exit_code == 0
    assert control.sites.get_site("cdn-example-com").ssl_automatic_mode == "custom"


def test_site_minimum_tls_and_cache_query_string_commands(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    tls = runner.invoke(
        cli.app,
        [
            "site",
            "minimum-tls",
            "cdn-example-com",
            "--version",
            "1.3",
            "--json",
        ],
    )
    query = runner.invoke(
        cli.app,
        [
            "site",
            "cache-query-string",
            "cdn-example-com",
            "--mode",
            "ignore",
            "--json",
        ],
    )

    assert tls.exit_code == 0
    assert json.loads(tls.stdout)["minimum_tls_version"] == "1.3"
    assert query.exit_code == 0
    assert json.loads(query.stdout)["cache_query_string_mode"] == "ignore"
    site = control.sites.get_site("cdn-example-com")
    assert site.minimum_tls_version == "1.3"
    assert site.cache_query_string_mode == "ignore"


def test_site_compression_command(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")
    assert control.sites.get_site("cdn-example-com").compression == "brotli"

    result = runner.invoke(
        cli.app,
        ["site", "compression", "cdn-example-com", "--mode", "gzip", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["compression"] == "gzip"
    assert control.sites.get_site("cdn-example-com").compression == "gzip"

    rejected = runner.invoke(
        cli.app,
        ["site", "compression", "cdn-example-com", "--mode", "deflate"],
    )

    assert rejected.exit_code != 0
    assert control.sites.get_site("cdn-example-com").compression == "gzip"


def test_site_visitor_headers_command(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")
    site = control.sites.get_site("cdn-example-com")
    assert site.visitor_headers.connecting_ip is True
    assert site.visitor_headers.ip_country is False

    result = runner.invoke(
        cli.app,
        ["site", "visitor-headers", "cdn-example-com", "--ip-country", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["visitor_headers"] == {
        "connecting_ip": True,
        "ip_country": True,
    }
    assert control.sites.get_site("cdn-example-com").visitor_headers.ip_country is True

    # An option that is not named keeps its value rather than resetting it.
    narrowed = runner.invoke(
        cli.app,
        [
            "site",
            "visitor-headers",
            "cdn-example-com",
            "--no-connecting-ip",
            "--json",
        ],
    )

    assert narrowed.exit_code == 0
    assert json.loads(narrowed.stdout)["visitor_headers"] == {
        "connecting_ip": False,
        "ip_country": True,
    }


def test_site_visitor_headers_requires_a_switch(settings, monkeypatch):
    """With no option the command would silently rewrite the block as-is."""
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["site", "visitor-headers", "cdn-example-com"])

    assert result.exit_code != 0


def test_site_visitor_headers_reports_what_the_origin_will_see(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="cdn-example-com", record="cdn", operator="cli")

    enabled = runner.invoke(
        cli.app,
        ["site", "visitor-headers", "cdn-example-com", "--ip-country"],
    )
    assert "BZ-Connecting-IP, BZ-IPCountry" in enabled.stdout

    off = runner.invoke(
        cli.app,
        [
            "site",
            "visitor-headers",
            "cdn-example-com",
            "--no-connecting-ip",
            "--no-ip-country",
        ],
    )
    assert "no BZ-* visitor headers" in off.stdout
    assert (
        control.sites.get_site("cdn-example-com").visitor_headers.connecting_ip is False
    )


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


def test_doctor_reports_a_resolver_that_invents_answers(settings, monkeypatch):
    _control(settings, monkeypatch)
    monkeypatch.setattr(
        diagnostics_cli,
        "check_resolver",
        lambda _settings: diagnostics_cli.ResolverCheck(
            passed=False,
            detail="resolver (host resolver) invents addresses",
        ),
    )

    result = runner.invoke(cli.app, ["doctor"])

    assert "invents addresses" in result.output


def test_doctor_can_skip_the_resolver_probe(settings, monkeypatch):
    """--no-resolver keeps doctor usable on a host with no DNS at all."""
    _control(settings, monkeypatch)

    def explode(_settings):
        raise AssertionError("the probe must not run with --no-resolver")

    monkeypatch.setattr(diagnostics_cli, "check_resolver", explode)

    result = runner.invoke(cli.app, ["doctor", "--no-resolver"])

    assert result.exit_code == 0
    assert "resolver" not in result.output


def test_version_reports_package_version():
    from blitzecdn import __version__

    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert f"blitzecdn {__version__}" in result.output
    assert "blitzecdn.edge" not in result.output


def _walk_commands(app, prefix="blitzecdn"):
    for command in app.registered_commands:
        name = command.name or command.callback.__name__.replace("_", "-")
        yield f"{prefix} {name}", (command.callback.__doc__ or command.help)
    for group in app.registered_groups:
        yield from _walk_commands(group.typer_instance, f"{prefix} {group.name}")


def test_every_command_has_help_text():
    """A blank row in `--help` is invisible until someone needs that command.

    Typer falls back to an empty description rather than failing, so four
    commands shipped undocumented without anything noticing. This is the only
    guard.
    """
    undocumented = [name for name, doc in _walk_commands(cli.app) if not doc]

    assert not undocumented, (
        f"these commands render a blank description in --help: {undocumented}"
    )


def test_group_help_mentions_the_derived_site_model():
    """The one thing a new operator gets wrong is looking for `site create`."""
    result = runner.invoke(cli.app, ["--help"])

    assert "You do not create virtual hosts" in strip_ansi(result.output)


def test_plugins_lists_what_is_installed_and_why_something_is_not(
    settings, monkeypatch
):
    """The command an operator runs after `pip install` and sees no new routes.

    A required capability that failed would have stopped the process, so the
    interesting half is the other one: an optional package that was installed,
    did not load, and was skipped by design. The reason is kept on the registry
    precisely so it can be answered here rather than only in a startup log line
    that has scrolled away.
    """
    control = _control(settings, monkeypatch)
    control.plugins.rejected = (
        PluginRejection("waf (blitzecdn_waf.plugin)", "import failed: no module"),
    )

    result = runner.invoke(cli.app, ["plugins", "--json"])
    document = json.loads(result.stdout)

    assert {"sites", "dns", "deployments"} <= {
        plugin["name"] for plugin in document["plugins"]
    }
    # `required` is what separates a capability this distribution ships from
    # one installed beside it, which is the first thing an operator wants to
    # read off this table.
    by_name = {plugin["name"]: plugin for plugin in document["plugins"]}
    assert by_name["sites"]["required"] is True
    assert by_name["sites"]["capabilities"] == ["sites"]
    assert "sites" in document["capabilities"]
    assert document["rejected"] == [
        {
            "source": "waf (blitzecdn_waf.plugin)",
            "reason": "import failed: no module",
        }
    ]


def test_plugins_names_a_skipped_package_on_stderr(settings, monkeypatch):
    """Not only in `--json`: the human output has to say it too.

    The whole reason a rejection is kept is that a warning at startup is not
    somewhere an operator can look afterwards. Printing the table and staying
    silent about the package that did not load would recreate that.
    """
    control = _control(settings, monkeypatch)
    control.plugins.rejected = (
        PluginRejection("waf (blitzecdn_waf.plugin)", "import failed: no module"),
    )

    result = runner.invoke(cli.app, ["plugins"])

    assert "waf (blitzecdn_waf.plugin) was not registered" in result.output
    assert "import failed: no module" in result.output
