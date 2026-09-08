"""What the CLI reports about itself: help, plugins, versions, doctor."""

import json
import sys
from pathlib import Path

import pytest
from cli_support import (
    _control,
    _seed_site,
    runner,
)
from click.utils import strip_ansi
from control_plane_fixtures import (
    FakeRunner,
    seed_site,
)

from blitzecdn.capabilities.diagnostics import cli as diagnostics_cli
from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.plugins import PluginRejection

REQUIRES_CAPABILITIES = {
    "test_plugins_shows_a_settings_value_and_only_a_secrets_presence": (
        "certificates",
        "security",
    ),
    "test_ansible_slots_answers_for_all_three_of_cores_plays": (
        "hardening",
        "resolver",
    ),
}


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

    An empty list is the report, not an omission: it is how a loopback-only
    listener looks, and reading it is how an operator confirms that closing the
    list actually closed it.
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

    assert {"dns", "deployments"} <= {plugin["name"] for plugin in document["plugins"]}
    # `required` is what separates a capability this distribution ships from
    # one installed beside it, which is the first thing an operator wants to
    # read off this table.
    by_name = {plugin["name"]: plugin for plugin in document["plugins"]}
    assert by_name["dns"]["required"] is True
    assert by_name["dns"]["capabilities"] == ["dns"]
    assert "dns" in document["capabilities"]
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


def test_plugins_shows_a_settings_value_and_only_a_secrets_presence(
    settings, monkeypatch
):
    """Two kinds of configuration, and the difference is what may be printed.

    This command prints to a terminal and into whatever captures its JSON, so
    a secret is only ever reported as set or unset. A setting is reported with
    its resolved value, which is the answer to "what is this controller
    actually using" — and the reason the two are declared as different things
    rather than as one list with a flag.
    """
    _control(settings, monkeypatch)

    result = runner.invoke(cli.app, ["plugins", "--json"])
    document = json.loads(result.stdout)
    configuration = {
        entry["name"]: entry
        for plugin in document["plugins"]
        for entry in plugin["configuration"]
    }

    secret = configuration["BLITZE_UNDER_ATTACK_SECRET"]
    assert secret["kind"] == "secret"
    assert secret["set"] is False
    assert "value" not in secret

    setting = configuration["BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS"]
    assert setting["kind"] == "setting"
    assert setting["value"] == "43200"


def test_ansible_roles_path_is_what_a_deployment_would_resolve():
    """Core's roles first, then each contributing distribution's, by plugin name.

    The justfile used to spell all nine directories out, so a checkout could
    syntax-check an edge play against a role set no deployment would resolve.
    Asking the same function the composition root calls is what removed that.
    """
    result = runner.invoke(cli.app, ["ansible", "roles-path"])
    entries = result.stdout.strip().split(":")

    # Core's is always first and is the only entry a core-only workspace has,
    # which is why this assertion needs no capability installed.
    assert entries[0].endswith("src/blitzecdn/ansible/roles")
    assert all(Path(entry).is_dir() for entry in entries)
    # Ordered by plugin name after core's, which is what makes two controllers
    # with the same packages installed resolve every role identically.
    contributed = [Path(entry).parents[2].name for entry in entries[1:]]
    assert contributed == sorted(contributed)


def test_ansible_slots_answers_for_all_three_of_cores_plays():
    """A slot is declared by the contributing package and cannot be read off
    this repository, which is why the hand-written copy had drifted in both
    directions: an edge role missing, and the teardown slot never passed.
    """
    result = runner.invoke(cli.app, ["ansible", "slots"])
    document = json.loads(result.stdout)

    assert set(document) == {
        "blitzecdn_capability_roles",
        "blitzecdn_host_capability_roles",
        "blitzecdn_edge_teardown_capability_roles",
    }
    # The one the justfile's literal omitted: `blitzecdn-resolver` declares an
    # edge role, and nothing in this repository says so except its own package.
    assert "blitzecdn_resolver" in document["blitzecdn_capability_roles"]
    assert document["blitzecdn_edge_teardown_capability_roles"]
    # Emitted even when empty, so a caller diffing this can see a slot is empty
    # rather than guess whether the question was asked.
    for roles in document.values():
        assert isinstance(roles, list)
