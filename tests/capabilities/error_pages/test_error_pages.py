"""Custom error pages and maintenance mode, from the contract to the edge.

The pages are files in the release, written by the capability's `page`
resources; the site's Nginx configuration points `error_page` at them from
site-scoped internal locations. These tests hold both halves: what is written
and what refers to it.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from pydantic import ValidationError
from support.cli import cli_control, runner
from support.contracts import role_defaults
from support.control_plane import cli_control_plane, seed_site
from support.edge_render import render_http_fragment, render_site, server_blocks
from support.paths import CORE_ANSIBLE
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.error_pages.contract import PAGE_LIMIT, ErrorPagesPolicy
from blitzecdn.capabilities.error_pages.feature import FEATURES
from blitzecdn.entrypoints.cli import main as cli

(FEATURE,) = FEATURES

PAGE = "<!doctype html><title>Down</title><p>We'll be right back.</p>"


def _site(**policy: Any) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": "shop-example",
                "server_names": ["shop.example.com"],
                "origin_host": "origin.example.com",
                **policy,
            }
        )
    )


def _named_location(rendered: str, name: str) -> str:
    match = re.search(
        r"^    location @" + re.escape(name) + r" \{\n(.*?)^    \}",
        rendered,
        re.M | re.S,
    )
    assert match, f"@{name} is not rendered"
    return match.group(1)


def _root_location(rendered: str) -> str:
    match = re.search(r"^    location / \{\n(.*?)^    \}", rendered, re.M | re.S)
    assert match
    return match.group(1)


# --- the contract ---------------------------------------------------------


def test_nothing_is_branded_or_paused_by_default():
    policy = ErrorPagesPolicy()

    assert policy.error_pages == {}
    assert policy.maintenance_mode is False
    assert policy.maintenance_page is None
    assert policy.maintenance_retry_after == 300
    assert policy.required_capabilities == frozenset()


def test_only_a_used_setting_requires_the_capability():
    assert ErrorPagesPolicy(error_pages={"502": PAGE}).capability_requirements == {
        "error_pages": ("error_pages",)
    }
    assert ErrorPagesPolicy(maintenance_mode=True).capability_requirements == {
        "error_pages": ("maintenance_mode",)
    }
    # A page kept ready for later asks for nothing yet.
    assert ErrorPagesPolicy(maintenance_page=PAGE).required_capabilities == frozenset()


def test_pages_are_keyed_by_a_supported_status_in_order():
    policy = ErrorPagesPolicy(error_pages={" 504 ": PAGE, "403": PAGE})

    assert list(policy.error_pages) == ["403", "504"]


@pytest.mark.parametrize("status", ["404", "200", "5xx", "999"])
def test_a_status_without_an_error_page_is_refused(status):
    with pytest.raises(ValidationError, match="not a status with an error page"):
        ErrorPagesPolicy(error_pages={status: PAGE})


def test_a_page_must_hold_something_and_fit():
    with pytest.raises(ValidationError, match="empty"):
        ErrorPagesPolicy(error_pages={"502": "  \n"})
    with pytest.raises(ValidationError, match="64 KiB"):
        ErrorPagesPolicy(maintenance_page="é" * (PAGE_LIMIT // 2 + 1))
    assert ErrorPagesPolicy(maintenance_page="a" * PAGE_LIMIT).maintenance_page


@pytest.mark.parametrize("seconds", [0, 7 * 24 * 3600 + 1])
def test_retry_after_is_bounded(seconds):
    with pytest.raises(ValidationError):
        ErrorPagesPolicy(maintenance_retry_after=seconds)


def test_feature_provides_error_pages():
    assert FEATURE.metadata.name == "error_pages"
    assert FEATURE.metadata.capabilities == frozenset({"error_pages"})


# --- the site's configuration ----------------------------------------------


def test_a_site_without_pages_renders_nothing_of_this_capability():
    rendered = render_site(_site())

    assert "error-pages" not in rendered
    assert "proxy_intercept_errors" not in rendered
    assert "maintenance" not in rendered


def test_error_pages_point_at_the_site_s_own_files():
    rendered = render_site(_site(error_pages={"502": PAGE, "403": PAGE}))

    for status in ("403", "502"):
        assert (
            f"    error_page {status} /.blitzecdn/internal/error-pages/{status}.html;"
            in rendered
        )
    location = re.search(
        r"location \^~ /\.blitzecdn/internal/error-pages/ \{\n(.*?)\n    \}",
        rendered,
        re.S,
    )
    assert location
    assert "internal;" in location.group(1)
    assert re.search(r"alias \S+/pages/shop-example/;", location.group(1))
    assert 'Cache-Control "no-store" always;' in location.group(1)


def test_only_origin_5xx_with_a_page_are_intercepted():
    rendered = render_site(
        _site(error_pages={"403": PAGE, "429": PAGE, "502": PAGE, "504": PAGE})
    )
    upstream = _named_location(rendered, "blitzecdn_upstream")

    assert "proxy_intercept_errors on;" in upstream
    assert re.findall(r"error_page (\d+)", upstream) == ["502", "504"]


def test_edge_rule_pages_alone_leave_origin_errors_untouched():
    upstream = _named_location(
        render_site(_site(error_pages={"403": PAGE})), "blitzecdn_upstream"
    )

    assert "proxy_intercept_errors" not in upstream


def test_under_attack_mode_keeps_the_5xx_pages(monkeypatch):
    """Its own error_page in the location would otherwise drop the server's."""
    upstream = _named_location(
        render_site(
            _site(error_pages={"502": PAGE}, under_attack_mode=True),
            blitzecdn_security_under_attack_enabled=True,
        ),
        "blitzecdn_upstream",
    )

    assert "error_page 401 = @blitzecdn_under_attack_redirect;" in upstream
    assert "error_page 502 /.blitzecdn/internal/error-pages/502.html;" in upstream
    # Interception would also turn an origin's own 401 into the challenge.
    assert "proxy_intercept_errors" not in upstream


def test_maintenance_answers_before_the_cache_or_origin():
    rendered = render_site(_site(maintenance_mode=True, maintenance_retry_after=120))
    root = _root_location(rendered)

    assert "rewrite ^ /.blitzecdn/internal/maintenance last;" in root
    assert root.index("rewrite") < root.index("try_files")
    maintenance = re.search(
        r"location = /\.blitzecdn/internal/maintenance \{\n(.*?)\n    \}",
        rendered,
        re.S,
    )
    assert maintenance
    assert "internal;" in maintenance.group(1)
    assert (
        "error_page 503 /.blitzecdn/internal/error-pages/maintenance.html;"
        in maintenance.group(1)
    )
    assert "return 503;" in maintenance.group(1)
    page = re.search(
        r"location = /\.blitzecdn/internal/error-pages/maintenance\.html \{\n(.*?)\n"
        r"    \}",
        rendered,
        re.S,
    )
    assert page
    assert "add_header Retry-After 120 always;" in page.group(1)
    assert re.search(r"alias \S+/pages/shop-example/maintenance\.html;", page.group(1))


def test_maintenance_exempts_allow_sources_through_a_per_site_geo():
    site = _site(maintenance_mode=True, firewall={"allow_sources": ["203.0.113.0/24"]})
    root = _root_location(render_site(site))
    http = render_http_fragment("error-pages-http.conf.j2", [site, _site()])

    assert "if ($blitzecdn_maintenance_shop_example) {" in root
    assert http.count("geo $blitzecdn_maintenance_") == 1
    geo = re.search(
        r"geo \$blitzecdn_maintenance_shop_example \{\n(.*?)\n\}", http, re.S
    )
    assert geo
    assert "default 1;" in geo.group(1)
    assert "203.0.113.0/24 0;" in geo.group(1)


def test_maintenance_keeps_the_https_redirect():
    rendered = render_site(
        _site(
            maintenance_mode=True,
            ssl_mode="full",
            always_use_https=True,
            certificate_mode="existing",
            certificate_path="/etc/ssl/certs/edge.pem",
            certificate_key_path="/etc/ssl/private/edge.key",
        )
    )
    plain, secure = server_blocks(rendered, role_defaults())

    assert plain and secure
    for block in plain:
        assert "rewrite ^ /.blitzecdn/internal/maintenance" not in block
        assert "return 301 https://$host$request_uri;" in block
    for block in secure:
        assert "rewrite ^ /.blitzecdn/internal/maintenance last;" in block


# --- the release ------------------------------------------------------------


@pytest.fixture
def shipped(settings):
    return settings.model_copy(update={"ansible_dir": CORE_ANSIBLE})


def test_a_release_carries_exactly_the_pages_a_site_uses(shipped, monkeypatch):
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(
        control,
        error_pages={"502": "<p>{{ not a template }}</p>"},
        maintenance_mode=True,
    )
    bundle = control.releases.render(control.releases.compile())
    pages = {
        name.rsplit("/", 1)[1]: content
        for name, content in bundle.files.items()
        if name.startswith("nginx/pages/example-com/")
    }

    assert sorted(pages) == ["502.html", "maintenance.html"]
    # An operator's page is data, never a template.
    assert pages["502.html"].decode().strip() == "<p>{{ not a template }}</p>"
    assert "Down for maintenance" in pages["maintenance.html"].decode()
    site = bundle.files["nginx/sites-available/example-com.conf"].decode()
    assert "nginx/pages/example-com/" in site


def test_maintenance_falls_back_to_the_503_page(shipped, monkeypatch):
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control, error_pages={"503": PAGE}, maintenance_mode=True)
    bundle = control.releases.render(control.releases.compile())

    assert (
        bundle.files["nginx/pages/example-com/maintenance.html"].decode().strip()
        == PAGE
    )


def test_a_release_without_pages_writes_no_pages(shipped, monkeypatch):
    control = cli_control_plane(shipped, monkeypatch)
    seed_site(control)
    bundle = control.releases.render(control.releases.compile())

    assert not [name for name in bundle.files if name.startswith("nginx/pages/")]


# --- the commands -----------------------------------------------------------


def test_error_pages_command_reads_the_files(settings, monkeypatch, tmp_path):
    control = cli_control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "setup")
    (tmp_path / "502.html").write_text(PAGE, encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "domain",
            "error-pages",
            "example.com",
            "--page",
            f"502={tmp_path / '502.html'}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["error_pages"] == {"502": PAGE}

    cleared = runner.invoke(
        cli.app, ["domain", "error-pages", "example.com", "--clear", "--json"]
    )
    assert cleared.exit_code == 0, cleared.output
    assert json.loads(cleared.stdout)["error_pages"] == {}


def test_error_pages_command_refuses_a_malformed_page_option(settings, monkeypatch):
    control = cli_control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "setup")

    result = runner.invoke(
        cli.app, ["domain", "error-pages", "example.com", "--page", "502"]
    )

    assert result.exit_code != 0


def test_maintenance_command_toggles_and_keeps_the_page(
    settings, monkeypatch, tmp_path
):
    control = cli_control(settings, monkeypatch)
    control.dns.create_domain(Domain(name="example.com"), "setup")
    (tmp_path / "maintenance.html").write_text(PAGE, encoding="utf-8")

    staged = runner.invoke(
        cli.app,
        [
            "domain",
            "maintenance",
            "example.com",
            "--page",
            str(tmp_path / "maintenance.html"),
            "--retry-after",
            "600",
            "--json",
        ],
    )
    assert staged.exit_code == 0, staged.output
    assert json.loads(staged.stdout)["maintenance_mode"] is False

    on = runner.invoke(
        cli.app, ["domain", "maintenance", "example.com", "--on", "--json"]
    )
    assert on.exit_code == 0, on.output
    document = json.loads(on.stdout)
    assert document["maintenance_mode"] is True
    assert document["maintenance_page"] == PAGE
    assert document["maintenance_retry_after"] == 600

    reset = runner.invoke(
        cli.app, ["domain", "maintenance", "example.com", "--default-page", "--json"]
    )
    assert reset.exit_code == 0, reset.output
    assert json.loads(reset.stdout)["maintenance_page"] is None
