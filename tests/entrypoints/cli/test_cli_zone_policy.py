"""The zone-policy switches: TLS, HTTP/3, caching, compression, uploads."""

import json

from cli_support import (
    _control,
    _seed_site,
    runner,
)
from control_plane_fixtures import (
    FakeRunner,
    seed_site,
)

from blitzecdn.cli import main as cli
from blitzecdn.composition import ControlPlane, Repository


def test_cli_always_use_https_toggle_drives_the_derived_site(settings, monkeypatch):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]
    monkeypatch.setattr(cli.common, "control_plane", lambda: control)
    runner.invoke(cli.app, ["domain", "add", "example.com"])
    _seed_site(control, "api", "198.51.100.20")

    sites = json.loads(runner.invoke(cli.app, ["domain", "hosts", "--json"]).stdout)
    assert sites[0]["always_use_https"] is False

    enabled = runner.invoke(
        cli.app,
        ["domain", "always-use-https", "example.com", "--on", "--json"],
    )

    assert enabled.exit_code == 0
    assert json.loads(enabled.stdout)["always_use_https"] is True
    sites = json.loads(runner.invoke(cli.app, ["domain", "hosts", "--json"]).stdout)
    assert sites[0]["always_use_https"] is True

    disabled = runner.invoke(
        cli.app,
        ["domain", "always-use-https", "example.com", "--off"],
    )
    assert disabled.exit_code == 0
    assert "now disabled" in disabled.stdout
    assert control.sites.get_site("example-com").always_use_https is False


def test_site_ssl_changes_the_combined_mode(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(
        control,
        name="example-com",
        record="cdn",
        operator="cli",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/edge.pem",
        certificate_key_path="/etc/ssl/private/edge.key",
    )

    result = runner.invoke(
        cli.app,
        ["domain", "ssl", "example.com", "--mode", "full_strict"],
    )

    assert result.exit_code == 0
    assert control.sites.get_site("example-com").ssl_mode == "full_strict"
    assert "Run 'blitzecdn deploy'" in result.stdout


def test_site_http3_toggles_quic_for_a_tls_site(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(
        control,
        name="example-com",
        record="cdn",
        operator="cli",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/certs/edge.pem",
        certificate_key_path="/etc/ssl/private/edge.key",
    )

    result = runner.invoke(
        cli.app, ["domain", "http3", "example.com", "--on", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["http3_enabled"] is True
    assert control.sites.get_site("example-com").http3_enabled is True


def test_site_under_attack_toggles_edge_mitigation(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app, ["domain", "under-attack", "example.com", "--on", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["under_attack_mode"] is True
    assert control.sites.get_site("example-com").under_attack_mode is True


def test_site_ssl_automatic_can_opt_out_to_custom(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app,
        [
            "domain",
            "ssl-automatic",
            "example.com",
            "--mode",
            "custom",
        ],
    )

    assert result.exit_code == 0
    assert control.sites.get_site("example-com").ssl_automatic_mode == "custom"


def test_site_minimum_tls_and_cache_query_string_commands(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    tls = runner.invoke(
        cli.app,
        [
            "domain",
            "minimum-tls",
            "example.com",
            "--version",
            "1.3",
            "--json",
        ],
    )
    query = runner.invoke(
        cli.app,
        [
            "domain",
            "cache-query-string",
            "example.com",
            "--mode",
            "ignore",
            "--json",
        ],
    )

    assert tls.exit_code == 0
    assert json.loads(tls.stdout)["minimum_tls_version"] == "1.3"
    assert query.exit_code == 0
    assert json.loads(query.stdout)["cache_query_string_mode"] == "ignore"
    site = control.sites.get_site("example-com")
    assert site.minimum_tls_version == "1.3"
    assert site.cache_query_string_mode == "ignore"


def test_site_max_upload_size_command(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    assert control.sites.get_site("example-com").max_upload_size == "100m"

    result = runner.invoke(
        cli.app,
        ["domain", "max-upload-size", "example.com", "--size", "200m", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["max_upload_size"] == "200m"
    assert control.sites.get_site("example-com").max_upload_size == "200m"

    rejected = runner.invoke(
        cli.app,
        ["domain", "max-upload-size", "example.com", "--size", "500m"],
    )

    assert rejected.exit_code != 0
    assert control.sites.get_site("example-com").max_upload_size == "200m"


def test_site_compression_command(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")
    assert control.sites.get_site("example-com").compression == "brotli"

    result = runner.invoke(
        cli.app,
        ["domain", "compression", "example.com", "--mode", "gzip", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["compression"] == "gzip"
    assert control.sites.get_site("example-com").compression == "gzip"

    rejected = runner.invoke(
        cli.app,
        ["domain", "compression", "example.com", "--mode", "deflate"],
    )

    assert rejected.exit_code != 0
    assert control.sites.get_site("example-com").compression == "gzip"


def test_site_cache_command_sets_the_switch_and_both_durations(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app,
        ["domain", "cache", "example.com", "--success", "4h", "--not-found", "30s"],
    )

    assert result.exit_code == 0
    site = control.sites.get_site("example-com")
    assert (site.cache_valid_success, site.cache_valid_not_found) == ("4h", "30s")
    assert site.cache_enabled is True, "a duration change must not touch the switch"


def test_site_cache_off_withdraws_the_claim_on_the_cache_capability(
    settings, monkeypatch
):
    """Turning caching off is what lets a core-only controller converge a site."""
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["domain", "cache", "example.com", "--off"])

    assert result.exit_code == 0
    site = control.sites.get_site("example-com")
    assert site.cache_enabled is False
    assert "cache" not in site.capability_requirements


def test_site_cache_refuses_a_duration_it_cannot_parse(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(
        cli.app, ["domain", "cache", "example.com", "--success", "forever"]
    )

    assert result.exit_code != 0
    assert control.sites.get_site("example-com").cache_valid_success == "10m"


def test_site_cache_with_no_option_says_what_to_name(settings, monkeypatch):
    control = _control(settings, monkeypatch)
    seed_site(control, name="example-com", record="cdn", operator="cli")

    result = runner.invoke(cli.app, ["domain", "cache", "example.com"])

    assert result.exit_code != 0
    assert "--on/--off" in result.output
