"""What the CLI reports about itself: help, features, versions, doctor."""

import json
import sys
from pathlib import Path

import pytest
from click.utils import strip_ansi
from support.cli import (
    _control,
    _seed_site,
    runner,
)
from support.control_plane import (
    FakeRunner,
    build_application,
    seed_site,
)

from blitzecdn.capabilities.diagnostics import cli as diagnostics_cli
from blitzecdn.composition import Repository
from blitzecdn.entrypoints.cli import main as cli

REQUIRES_CAPABILITIES = {
    "test_features_shows_a_settings_value_and_only_a_secrets_presence": (
        "certificates",
        "security",
    ),
    "test_ansible_slots_answers_for_all_three_of_cores_plays": (
        "hardening",
        "resolver",
    ),
}


def test_cli_domain_record_status_audit_and_doctor(settings, monkeypatch, tmp_path):
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli.common, "services", lambda: control)
    monkeypatch.setattr(cli.common, "settings", lambda: settings)
    assert runner.invoke(cli.app, ["domain", "add", "example.com"]).exit_code == 0
    _seed_site(control, "cdn", "198.51.100.10")
    listed = runner.invoke(cli.app, ["domain", "list", "--json"])
    assert listed.exit_code == 0 and "example.com" in listed.stdout
    hosts = runner.invoke(cli.app, ["domain", "hosts", "--json"])
    assert hosts.exit_code == 0 and "example-com" in hosts.stdout
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


def test_domain_show_reveals_defaults_nothing_ever_mentioned(settings, monkeypatch):
    """A site is derived, so its resolved policy is not visible on the record."""
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["domain", "hosts", "--json"])

    assert result.exit_code == 0
    (site,) = json.loads(result.stdout)
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
    monkeypatch.setattr(sys, "argv", ["blitzecdn", "domain", "show", "absent"])

    with pytest.raises(SystemExit) as exit_info:
        cli.run()

    assert exit_info.value.code == cli.ExitCode.NOT_FOUND


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


def test_doctor_reports_who_the_api_answers(settings, monkeypatch):
    """The question `doctor` exists to answer, asked about network exposure.

    An empty list is how a loopback-only listener looks.
    """
    _control(settings, monkeypatch)
    closed = runner.invoke(cli.app, ["doctor", "--no-resolver", "--json"])
    assert json.loads(closed.stdout)["api_allowed_ips"] == []

    _control(
        settings.model_copy(
            update={"allowed_ips": ("203.0.113.8/32", "198.51.100.0/24")}
        ),
        monkeypatch,
    )
    opened = runner.invoke(cli.app, ["doctor", "--no-resolver", "--json"])
    assert json.loads(opened.stdout)["api_allowed_ips"] == [
        "203.0.113.8/32",
        "198.51.100.0/24",
    ]


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

    Typer falls back to an empty description, so this is the only guard.
    """
    undocumented = [name for name, doc in _walk_commands(cli.app) if not doc]

    assert not undocumented, (
        f"these commands render a blank description in --help: {undocumented}"
    )


def test_group_help_mentions_the_derived_site_model():
    """The one thing a new operator gets wrong is looking for `site create`."""
    result = runner.invoke(cli.app, ["--help"])

    assert "You do not create virtual hosts" in strip_ansi(result.output)


def test_features_lists_every_feature_and_the_tokens_it_answers_for(
    settings, monkeypatch
):
    """The table an operator reads to see what this controller has."""
    _control(settings, monkeypatch)

    result = runner.invoke(cli.app, ["features", "--json"])
    document = json.loads(result.stdout)

    assert {"dns", "deployments"} <= {
        feature["name"] for feature in document["features"]
    }
    by_name = {feature["name"]: feature for feature in document["features"]}
    assert by_name["dns"]["capabilities"] == ["dns"]
    assert "dns" in document["capabilities"]
    assert "rejected" not in document


def test_features_shows_a_settings_value_and_only_a_secrets_presence(
    settings, monkeypatch
):
    """Two kinds of configuration, and the difference is what may be printed.

    A secret is only reported as set or unset; a setting with its resolved value.
    """
    _control(settings, monkeypatch)

    result = runner.invoke(cli.app, ["features", "--json"])
    document = json.loads(result.stdout)
    configuration = {
        entry["name"]: entry
        for feature in document["features"]
        for entry in feature["configuration"]
    }

    secret = configuration["BLITZE_UNDER_ATTACK_SECRET"]
    assert secret["kind"] == "secret"
    assert secret["set"] is False
    assert "value" not in secret

    setting = configuration["BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"]
    assert setting["kind"] == "setting"
    assert setting["value"] == "43200"


def test_ansible_roles_path_is_what_a_deployment_would_resolve():
    """Core's roles first, then each contributor's, by feature name.

    Computed by the same function the composition root calls.
    """
    result = runner.invoke(cli.app, ["ansible", "roles-path"])
    entries = result.stdout.strip().split(":")

    # Core's is always first.
    assert entries[0].endswith("src/blitzecdn/assets/ansible/roles")
    assert all(Path(entry).is_dir() for entry in entries)
    # Ordered by feature name after core's, which is what makes two controllers
    # with the same packages installed resolve every role identically.
    from blitzecdn.composition import load_registry

    contributions = load_registry().ansible_contributions()
    expected = [
        str(item.roles_path)
        for item in sorted(contributions, key=lambda item: item.feature)
    ]
    assert entries[1:] == list(dict.fromkeys(expected))


def test_ansible_slots_answers_for_all_three_of_cores_plays():
    """Slots are declared by the contributing features, so they are read from the
    registry rather than written by hand.
    """
    result = runner.invoke(cli.app, ["ansible", "slots"])
    document = json.loads(result.stdout)

    assert set(document) == {
        "blitzecdn_capability_roles",
        "blitzecdn_host_capability_roles",
        "blitzecdn_edge_teardown_capability_roles",
    }
    # The resolver capability declares an edge role; only the registry says so.
    assert "blitzecdn_resolver" in document["blitzecdn_capability_roles"]
    assert document["blitzecdn_edge_teardown_capability_roles"]
    # Emitted even when empty, so a caller diffing this can see a slot is empty
    # rather than guess whether the question was asked.
    for roles in document.values():
        assert isinstance(roles, list)
