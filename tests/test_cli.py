import json
import sys

import pytest
from click.utils import strip_ansi
from conftest import (
    FakePreflight,
    FakeRunner,
    ansible_run,
    host_run,
    origin_report,
)
from typer.testing import CliRunner

from blitzecdn import cli
from blitzecdn.control_plane import ControlPlane
from blitzecdn.domain.certificates import PreflightCheck, PreflightSeverity
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.runs import RunStatus
from blitzecdn.domain.sites import CdnSite
from blitzecdn.infrastructure.database import Repository

runner = CliRunner()


def test_init_creates_private_environment_file(tmp_path):
    output = tmp_path / "generated.env"
    result = runner.invoke(cli.app, ["init", "--output", str(output)])
    assert result.exit_code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert "BLITZE_API_KEYS=local:" in output.read_text(encoding="utf-8")
    duplicate = runner.invoke(cli.app, ["init", "--output", str(output)])
    assert duplicate.exit_code == 2


def test_cli_domain_record_status_audit_and_doctor(settings, monkeypatch, tmp_path):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    assert runner.invoke(cli.app, ["domain", "add", "example.com"]).exit_code == 0
    assert (
        runner.invoke(
            cli.app,
            [
                "record",
                "add",
                "example.com",
                "cdn",
                "--value",
                "198.51.100.10",
                "--proxied",
            ],
        ).exit_code
        == 0
    )
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
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
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
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "record", "list", "absent.example"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.INVALID_INPUT
    assert "does not exist" in capsys.readouterr().err


def test_cli_proxy_toggle_drives_the_derived_site(settings, monkeypatch):
    """`record proxy --on/--off` is the CDN switch for one subdomain."""
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    added = runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20", "--json"],
    )
    assert added.exit_code == 0
    assert json.loads(added.stdout)["proxied"] is False
    # Unproxied, the edge knows nothing about it.
    assert json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout) == []

    toggled = runner.invoke(
        cli.app, ["record", "proxy", "example.com", "api", "--on", "--json"]
    )
    assert toggled.exit_code == 0
    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert [site["server_names"] for site in sites] == [["api.example.com"]]
    assert sites[0]["origin_host"] == "198.51.100.20"

    runner.invoke(cli.app, ["record", "proxy", "example.com", "api", "--off"])
    assert json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout) == []


def test_cli_firewall_replaces_only_the_lists_it_names(settings, monkeypatch):
    """Merge semantics, and the derived site carries the result.

    The CLI keeps the lists the operator did not mention, unlike the API PATCH
    that replaces the whole block. Getting this wrong would silently drop the
    rules a second invocation did not repeat.
    """
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        [
            "record",
            "add",
            "example.com",
            "api",
            "--value",
            "198.51.100.20",
            "--proxied",
        ],
    )

    first = runner.invoke(
        cli.app,
        [
            "record",
            "firewall",
            "example.com",
            "api",
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
        ["record", "firewall", "example.com", "api", "--deny-country", "ru", "--json"],
    )
    assert second.exit_code == 0
    firewall = json.loads(second.stdout)["firewall"]
    assert firewall["deny_sources"] == ["203.0.113.0/24"]
    assert firewall["denied_paths"] == ["/admin"]
    assert firewall["denied_countries"] == ["RU"]

    sites = json.loads(runner.invoke(cli.app, ["site", "list", "--json"]).stdout)
    assert sites[0]["firewall"]["denied_countries"] == ["RU"]

    cleared = runner.invoke(
        cli.app, ["record", "firewall", "example.com", "api", "--clear", "--json"]
    )
    assert json.loads(cleared.stdout)["firewall"]["deny_sources"] == []


def test_cli_firewall_refuses_a_network_with_host_bits_set(settings, monkeypatch):
    """203.0.113.5/24 means one address to the operator and 256 to nginx."""
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        ["record", "add", "example.com", "api", "--value", "198.51.100.20"],
    )
    result = runner.invoke(
        cli.app,
        [
            "record",
            "firewall",
            "example.com",
            "api",
            "--deny-source",
            "203.0.113.5/24",
        ],
    )
    assert result.exit_code != 0


def test_cli_firewall_requires_a_rule_or_clear(settings, monkeypatch):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app, ["record", "add", "example.com", "api", "--value", "198.51.100.20"]
    )
    bare = runner.invoke(cli.app, ["record", "firewall", "example.com", "api"])
    assert bare.exit_code != 0
    conflicting = runner.invoke(
        cli.app,
        [
            "record",
            "firewall",
            "example.com",
            "api",
            "--clear",
            "--deny-path",
            "/admin",
        ],
    )
    assert conflicting.exit_code != 0


def test_cli_dns_export_hides_addresses_for_proxied_records(settings, monkeypatch):
    control = ControlPlane(settings, Repository(settings.database_path), FakeRunner())  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    runner.invoke(
        cli.app,
        [
            "record",
            "add",
            "example.com",
            "api",
            "--value",
            "198.51.100.20",
            "--proxied",
        ],
    )
    exported = json.loads(runner.invoke(cli.app, ["dns", "export", "--json"]).stdout)
    assert exported[0]["proxied"] is True
    assert "value" not in exported[0]


def test_cli_plan_deploy_status_and_rollback(settings, site_payload, monkeypatch):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
            ansible_run(host_run("edge-a")),
        ]
    )
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
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


def test_deploy_can_issue_ready_certificates_and_install_them(
    settings, site_payload, monkeypatch
):
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)
    repository.sites.create_site(site)
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
    report = FakePreflight().check(site, deployed=True, record_ttl=300)
    requested: list[str] = []
    monkeypatch.setattr(
        control.certificates, "certificate_preflight", lambda _name: report
    )
    monkeypatch.setattr(
        control.certificates,
        "request_certificate",
        lambda name, _operator, *_args, **_kwargs: requested.append(name),
    )
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    result = runner.invoke(
        cli.app, ["deploy", "--yes", "--request-certificates", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert requested == [site.name]
    assert payload["certificates_issued"] == [site.name]
    assert payload["certificate_deployment"]["status"] == "succeeded"


def test_deploy_refuses_certificate_issuance_for_a_canary(settings, monkeypatch):
    _control(settings, monkeypatch)
    result = runner.invoke(
        cli.app,
        ["deploy", "--yes", "--limit", "edge-01", "--request-certificates"],
        color=False,
    )
    assert result.exit_code == 2
    assert "cannot be combined with --limit" in strip_ansi(result.output)


def test_deploy_does_not_contact_ca_when_certificate_preflight_blocks(
    settings, site_payload, monkeypatch
):
    repository = Repository(settings.database_path)
    site = CdnSite.model_validate(site_payload)
    repository.sites.create_site(site)
    control = ControlPlane(
        settings,
        repository,
        FakeRunner([ansible_run(host_run("edge-a"))]),
    )  # type: ignore[arg-type]
    report = FakePreflight(("dns",)).check(site, deployed=True, record_ttl=300)
    monkeypatch.setattr(
        control.certificates, "certificate_preflight", lambda _name: report
    )

    def _unexpected_request(_name, _operator):
        raise AssertionError("the CA must not be contacted after a blocked preflight")

    monkeypatch.setattr(
        control.certificates, "request_certificate", _unexpected_request
    )
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)

    result = runner.invoke(
        cli.app, ["deploy", "--yes", "--request-certificates", "--json"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["certificates_issued"] == []
    assert "dns" in payload["certificates_skipped"][site.name]
    assert payload["certificate_deployment"] is None


def test_interactive_deploy_validates_previews_and_applies(
    settings, site_payload, monkeypatch
):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    # `validate` reads the first result without consuming it, so one entry
    # serves both it and the preview; the second is the apply.
    fake = FakeRunner(
        [
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
            ansible_run(host_run("edge-a", changes=("Render managed sites",))),
        ]
    )
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
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
    control = ControlPlane(
        settings,
        Repository(settings.database_path),
        runner_double or FakeRunner(),
        preflight=preflight or FakePreflight(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    return control


def _store(settings):
    """A repository on the same database, for seeding and for reading back.

    The control plane does not hand out its stores — that is the point of the
    layering rule — so a test that wants to plant a derived site or read the
    audit trail straight from SQLite opens its own handle on the same file
    rather than reaching through the object under test.
    """
    return Repository(settings.database_path)


def _seed_certificate(control, certificate_pair, *, days):
    """Create the proxied record a certificate has to belong to, and upload one."""
    control.dns.create_domain(Domain(name="example.com"), "cli")
    record = control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "cli",
    )
    certificate, key = certificate_pair((record.fqdn,), days=days)
    control.certificates.upload_certificate(record.site_name, certificate, key, "cli")
    return record.site_name


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
    info = control.certificate_store.get(site_name)
    path.write_text(
        info.model_copy(update={"not_after": info.not_before}).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["cert", "list"])

    assert result.exit_code == cli.ExitCode.CONFLICT


def _seed_origin_site(settings):
    _store(settings).sites.create_site(
        CdnSite.model_validate(
            {
                "name": "cdn-example-com",
                "server_names": ["cdn.example.com"],
                "origin_host": "origin.example.com",
            }
        )
    )


def test_origin_check_names_the_edges_that_could_not_reach_an_origin(
    settings, monkeypatch
):
    """The fleet answers, so the answer is per edge as well as per site.

    An origin no edge can reach is down; one only some edges can reach is a
    routing or allow-list problem. Reporting which edges failed is what lets an
    operator tell those apart, and is the whole reason the check moved off the
    controller.
    """
    _control(
        settings,
        monkeypatch,
        FakeRunner(
            [
                ansible_run(
                    origin_report("edge-a"),
                    origin_report("edge-b", reachable=False, detail="timed out"),
                )
            ]
        ),
    )
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "cdn-example-com: unreachable from edge-b" in result.output
    assert "edge-a" not in result.output.split("unreachable from")[1]


def test_origin_check_passes_when_every_edge_reaches_every_origin(
    settings, monkeypatch
):
    _control(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))]),
    )
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "answered as expected" in result.output


def test_origin_check_reports_an_edge_that_said_nothing(settings, monkeypatch):
    """A silent edge is not a passing edge."""
    _control(
        settings,
        monkeypatch,
        FakeRunner([ansible_run(host_run("edge-a", unreachable=1))]),
    )
    _seed_origin_site(settings)

    result = runner.invoke(cli.app, ["origin", "check"])

    assert result.exit_code == 0
    assert "edge-a: unreachable" in result.output


def test_doctor_surfaces_certificates_close_to_expiry(
    settings, monkeypatch, certificate_pair
):
    control = _control(settings, monkeypatch)
    _seed_certificate(control, certificate_pair, days=7)

    result = runner.invoke(cli.app, ["doctor"])

    assert "certificate(s) expire within" in result.output


def _renewal(*, renewed=(), skipped=(), failed=()):
    """Stand in for a real renewal run, which would reach a CA over the network."""
    return {
        "renewed": list(renewed),
        "skipped": list(skipped),
        "failed": list(failed),
    }


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


def test_plan_exits_five_when_check_mode_fails(settings, site_payload, monkeypatch):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    control = ControlPlane(
        settings,
        repository,
        FakeRunner(
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
    settings, site_payload, monkeypatch
):
    repository = Repository(settings.database_path)
    repository.sites.create_site(CdnSite.model_validate(site_payload))
    fake = FakeRunner([ansible_run(host_run("edge-a")) for _ in range(2)])
    control = ControlPlane(settings, repository, fake)  # type: ignore[arg-type]
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
        cli.diagnostics.uvicorn, "run", lambda *a, **k: started.append(a)
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code != 0
    assert started == []


def test_site_show_reveals_defaults_a_record_never_mentioned(settings, monkeypatch):
    """A site is derived, so its resolved policy is not visible on the record."""
    control = _control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "cli")
    control.dns.create_record(
        DnsRecord(
            domain="example.com", name="cdn", value="198.51.100.10", proxied=True
        ),
        "cli",
    )

    result = runner.invoke(cli.app, ["site", "show", "cdn-example-com", "--json"])

    assert result.exit_code == 0
    site = json.loads(result.stdout)
    assert site["server_names"] == ["cdn.example.com"]
    assert site["origin_host"] == "198.51.100.10"
    # Never set on the record; only the derived site shows them.
    assert site["origin_scheme"] == "https"
    assert site["cache_valid_success"] == "10m"


def test_site_show_reports_an_unknown_site_without_a_traceback(settings, monkeypatch):
    _control(settings, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "site", "show", "absent"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.INVALID_INPUT


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


# ----------------------------------------------------------------------
# cache purge / stats
# ----------------------------------------------------------------------


def _purge_ok():
    return ansible_run(host_run("edge-a", changed=1))


def _purgeable_site(settings):
    """A site serving TLS, because these tests purge `https://` URLs.

    The scheme leads the cache key, so the control plane refuses a purge for a
    scheme the site never serves. A site without TLS caches nothing under
    https.
    """
    _store(settings).sites.create_site(
        CdnSite.model_validate(
            {
                "name": "cdn-example-com",
                "server_names": ["cdn.example.com"],
                "origin_host": "o.example.com",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/cdn.pem",
                "certificate_key_path": "/etc/ssl/private/cdn.key",
            }
        )
    )


def test_cache_purge_sends_the_url_split_into_host_and_uri(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    _control(settings, monkeypatch, fake)
    _purgeable_site(settings)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/app.js"]
    )

    assert result.exit_code == 0
    assert fake.purges[0][0] == [
        {"host": "cdn.example.com", "uri": "/app.js", "scheme": "https"}
    ]


def test_cache_purge_keeps_the_query_string(settings, monkeypatch):
    """It is part of $request_uri, so '/a' and '/a?v=2' are different entries."""
    fake = FakeRunner([_purge_ok()])
    _control(settings, monkeypatch, fake)
    _purgeable_site(settings)

    runner.invoke(cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a?v=2"])

    assert fake.purges[0][0][0]["uri"] == "/a?v=2"


def test_cache_purge_defaults_a_bare_url_to_https_and_root(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    _control(settings, monkeypatch, fake)
    _purgeable_site(settings)

    runner.invoke(cli.app, ["cache", "purge", "--url", "cdn.example.com"])

    assert fake.purges[0][0] == [
        {"host": "cdn.example.com", "uri": "/", "scheme": "https"}
    ]


def test_cache_purge_rejects_a_url_with_no_hostname(settings, monkeypatch):
    _control(settings, monkeypatch)
    result = runner.invoke(cli.app, ["cache", "purge", "--url", "https:///app.js"])
    assert result.exit_code != 0


def test_cache_purge_all_asks_before_emptying_the_cache(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    _control(settings, monkeypatch, fake)

    declined = runner.invoke(cli.app, ["cache", "purge", "--all"], input="n\n")

    assert declined.exit_code == 1
    assert fake.purges == []


def test_cache_purge_all_proceeds_with_yes(settings, monkeypatch):
    fake = FakeRunner([_purge_ok()])
    _control(settings, monkeypatch, fake)

    result = runner.invoke(cli.app, ["cache", "purge", "--all", "--yes"])

    assert result.exit_code == 0
    assert fake.purges[0][1] is True


def test_cache_purge_exits_five_when_an_edge_did_not_purge(settings, monkeypatch):
    """A partial purge must not read as success to a script."""
    partial = ansible_run(
        host_run("edge-a", changed=1), host_run("edge-b", ok=0, unreachable=1)
    )
    _control(settings, monkeypatch, FakeRunner([partial]))
    _purgeable_site(settings)

    result = runner.invoke(
        cli.app, ["cache", "purge", "--url", "https://cdn.example.com/a.js"]
    )

    assert result.exit_code == cli.ExitCode.DEPLOYMENT_FAILED
    assert "edge-b did not purge" in result.output


def test_cache_purge_reports_an_unserved_host_without_a_traceback(
    settings, monkeypatch
):
    _control(settings, monkeypatch, FakeRunner([_purge_ok()]))
    _purgeable_site(settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["blitzecdn", "cache", "purge", "--url", "https://nope.example.com/x"],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.INVALID_INPUT


def _stats_runner(*hosts):
    """Counters arrive on the run itself, published by the role as a fact."""
    return FakeRunner([ansible_run(*hosts)])


_EDGE_REPORT = {
    "host": "edge-a",
    "collected_at": "2026-08-09T01:00:00Z",
    "nginx_reachable": True,
    "connections": {"active": 3},
    "cache": [
        {"site": "cdn.example.com", "outcome": "HIT", "requests": 3},
        {"site": "cdn.example.com", "outcome": "MISS", "requests": 1},
    ],
}


def test_stats_reports_the_fleet_hit_ratio(settings, monkeypatch):
    _control(
        settings,
        monkeypatch,
        _stats_runner(host_run("edge-a", ok=5, report=_EDGE_REPORT)),
    )

    result = runner.invoke(cli.app, ["stats", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["hit_ratio"] == 0.75
    assert payload["edges"][0]["host"] == "edge-a"
    assert "sites" not in payload


def test_stats_by_site_breaks_the_numbers_down(settings, monkeypatch):
    _control(
        settings,
        monkeypatch,
        _stats_runner(host_run("edge-a", ok=5, report=_EDGE_REPORT)),
    )

    payload = json.loads(
        runner.invoke(cli.app, ["stats", "--by-site", "--json"]).stdout
    )

    assert payload["sites"][0]["site"] == "cdn.example.com"
    assert payload["sites"][0]["hit_ratio"] == 0.75


def test_stats_says_so_when_there_is_no_cacheable_traffic_yet(settings, monkeypatch):
    """A fresh edge must not be reported as a cache that is failing."""
    quiet = {**_EDGE_REPORT, "cache": [{"site": "a", "outcome": "NONE", "requests": 4}]}
    _control(
        settings, monkeypatch, _stats_runner(host_run("edge-a", ok=5, report=quiet))
    )

    result = runner.invoke(cli.app, ["stats"])

    assert result.exit_code == 0
    assert "no hit ratio yet" in result.output


def test_stats_names_an_edge_that_reported_nothing(settings, monkeypatch):
    _control(
        settings,
        monkeypatch,
        _stats_runner(
            host_run("edge-a", ok=5, report=_EDGE_REPORT),
            host_run("edge-b", ok=0, unreachable=1),
        ),
    )

    result = runner.invoke(cli.app, ["stats"])

    assert "edge-b reported nothing: unreachable" in result.output


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
        cli.diagnostics,
        "check_resolver",
        lambda _settings: PreflightCheck(
            name="resolver",
            passed=False,
            severity=PreflightSeverity.BLOCKING,
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

    monkeypatch.setattr(cli.diagnostics, "check_resolver", explode)

    result = runner.invoke(cli.app, ["doctor", "--no-resolver"])

    assert result.exit_code == 0
    assert "resolver" not in result.output


def test_version_reports_all_three_contract_versions():
    """`--version` answers the question a schema-skew failure raises.

    A role refusing `blitzecdn_desired_state_version` means the control plane
    and the edge collection disagree. Reporting only the package version would
    leave the operator to dig the other two out of `ansible/requirements.yml`
    and the domain model.
    """
    from blitzecdn import __version__
    from blitzecdn.domain.sites import DESIRED_STATE_VERSION

    result = runner.invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert f"blitzecdn {__version__}" in result.output
    assert "blitzecdn.edge" not in result.output
    assert f"desired-state schema {DESIRED_STATE_VERSION}" in result.output


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
