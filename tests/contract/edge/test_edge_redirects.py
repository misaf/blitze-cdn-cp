"""Always Use HTTPS, and Under Attack Mode, across every listener."""

from __future__ import annotations

from typing import Any

import pytest
from support.contracts import (
    role_defaults,
)
from support.edge_render import (
    ROLE_DIR,
    mode_site,
    render_site,
    rendered_upstreams,
    role_spec,
    server_blocks,
)
from support.paths import REPO_ROOT
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.tls.contract import (
    SslMode,
    TlsPolicy,
)

REQUIRES_CAPABILITIES = {
    "test_under_attack_mode_redirects_permanently_on_every_challenge_path": (
        "security",
    ),
    "test_under_attack_mode_renders_before_redirect_and_proxy_on_http_and_https": (
        "security",
    ),
    "test_under_attack_reserved_endpoints_are_edge_only_and_uncached": ("security",),
    "test_acme_bypasses_under_attack_mode_in_every_server_block": ("security",),
}


def test_always_use_https_redirects_http_when_enabled():
    site = site_from_settings(
        {
            "name": "redirect",
            "server_names": ["redirect.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "always_use_https": True,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    rendered = render_site(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" in rendered


def test_always_use_https_uses_a_permanent_redirect():
    """Cloudflare-compatible status code: 301, not 308.

    Cloudflare answers Always Use HTTPS with 301. Every redirecting path (plain
    HTTP, both Under Attack Mode endpoints, the named redirect location) uses it,
    so the template is checked whole.
    """
    template = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    security_template = (
        REPO_ROOT
        / "src/blitzecdn/capabilities/security/nginx"
        / "security-server.conf.j2"
    ).read_text(encoding="utf-8")
    implementation = template + security_template

    assert "return 30" in implementation
    assert "return 308" not in implementation
    assert implementation.count("return 301 https://$host$request_uri;") == 3


def test_always_use_https_redirects_every_http_listener_without_its_port():
    """Redirect semantics are unchanged by port preservation, deliberately.

    The HTTP and HTTPS proxy ports are independent sets with no mapping, so the
    redirect stays scheme-only. ``$host`` carries no port, landing on 443, which a
    TLS site always serves, so it cannot loop.
    """
    site = mode_site(SslMode.FULL, serves_tls=True, always_use_https=True)
    rendered = render_site(site_to_ansible(site))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = server_blocks(rendered, defaults)

    ports = runtime["listeners"]["http"] + runtime["listeners"]["https"]
    assert len(http_blocks) == len(runtime["listeners"]["http"])
    for block in http_blocks:
        assert "return 301 https://$host$request_uri;" in block
        assert "proxy_pass" not in block
        assert "$blitzecdn_upstream" not in block
        # No port is carried into the Location, from either set.
        for port in ports:
            assert f"https://$host:{port}" not in block
    # The HTTPS listeners still proxy, each to its own port.
    assert rendered_upstreams(rendered) == {
        f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]
    }
    assert len(https_blocks) == len(runtime["listeners"]["https"])


def _redirects(rendered: str) -> bool:
    return "return 301 https://$host$request_uri;" in rendered


@pytest.mark.parametrize("under_attack", [False, True])
@pytest.mark.parametrize(
    "mode", [SslMode.OFF, SslMode.FLEXIBLE, SslMode.FULL, SslMode.FULL_STRICT]
)
def test_the_template_agrees_with_the_domains_redirect_rule(mode, under_attack):
    """The redirect rule is written twice, so the two are pinned together.

    The template gates on ``tls and always_use_https``; the control plane answers
    ``CdnSite.redirects_http_to_https``. Checked for every mode in both the normal
    and Under Attack Mode flow.
    """
    site = mode_site(
        mode,
        serves_tls=mode is not SslMode.OFF,
        always_use_https=True,
        under_attack_mode=under_attack,
    )
    rendered = render_site(
        site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=under_attack
    )

    assert _redirects(rendered) is site.redirects_http_to_https


def test_ssl_off_ignores_always_use_https_instead_of_looping():
    """Off serves no HTTPS listener, so the redirect must not be emitted.

    The stored preference is kept but inert, as Cloudflare does; a cached 301 to
    a dead port would be worse.
    """
    site = mode_site(SslMode.OFF, serves_tls=False, always_use_https=True)
    rendered = render_site(site_to_ansible(site))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = server_blocks(rendered, defaults)

    assert site.policy[TlsPolicy].always_use_https is True
    assert site.redirects_http_to_https is False
    assert not https_blocks
    assert "listen 443" not in rendered
    assert not _redirects(rendered)
    # Every HTTP listener still proxies, over HTTP, to its own port.
    assert rendered_upstreams(rendered) == {
        f"http://origin.example.com:{port}" for port in runtime["listeners"]["http"]
    }
    for block in http_blocks:
        assert "proxy_pass" in block


def test_under_attack_mode_redirects_permanently_on_every_challenge_path():
    """The mitigation endpoints redirect with the same code as the proxy path."""
    site = mode_site(
        SslMode.FULL,
        serves_tls=True,
        always_use_https=True,
        under_attack_mode=True,
    )
    rendered = render_site(
        site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=True
    )
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, _ = server_blocks(rendered, defaults)

    assert "return 308" not in rendered
    for block in http_blocks:
        assert "proxy_pass" not in block
        # challenge, verify, and the guarded fall-through, per HTTP listener.
        assert block.count("return 301 https://$host$request_uri;") == 3
    assert rendered_upstreams(rendered) == {
        f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]
    }


def test_always_use_https_can_be_disabled_without_disabling_tls():
    site = site_from_settings(
        {
            "name": "both-schemes",
            "server_names": ["both.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "always_use_https": False,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    rendered = render_site(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2
    assert "listen 443 ssl;" in rendered


def _under_attack_site(*, enabled: bool = True) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": "mitigated",
                "server_names": ["mitigated.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
                "under_attack_mode": enabled,
                "always_use_https": True,
            }
        )
    )


def test_under_attack_mode_is_absent_from_the_disabled_request_flow():
    rendered = render_site(_under_attack_site(enabled=False))

    assert "blitzecdn_under_attack.guard" not in rendered
    assert "X-BlitzeCDN-Mitigation challenge" not in rendered
    assert "auth_request /.blitzecdn/" not in rendered
    assert "proxy_pass" in rendered


def test_under_attack_mode_renders_before_redirect_and_proxy_on_http_and_https():
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = render_site(
        _under_attack_site(), blitzecdn_nginx_under_attack_enabled=True
    )

    assert rendered.count("auth_request /.blitzecdn/internal/under-attack-guard;") == (
        len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])
    )
    assert "try_files /__blitzecdn_dispatch__ @blitzecdn_upstream;" in rendered
    assert "proxy_pass" in rendered
    assert rendered.index("auth_request /.blitzecdn/") < rendered.index("proxy_pass")
    assert "X-BlitzeCDN-Mitigation challenge" in rendered


def test_under_attack_reserved_endpoints_are_edge_only_and_uncached():
    rendered = render_site(
        _under_attack_site(), blitzecdn_nginx_under_attack_enabled=True
    )

    assert "location = /.blitzecdn/challenge {" in rendered
    assert "location = /.blitzecdn/challenge/verify {" in rendered
    assert "location ^~ /.blitzecdn/" in rendered
    assert "js_content blitzecdn_under_attack.challenge;" in rendered
    assert "js_content blitzecdn_under_attack.verify;" in rendered
    assert 'Cache-Control "no-store' in rendered
    assert "limit_req zone=blitzecdn_under_attack_verify" in rendered

    for marker in (
        "location = /.blitzecdn/challenge {",
        "location = /.blitzecdn/challenge/verify {",
    ):
        block = rendered.split(marker, 1)[1].split("    }", 1)[0]
        assert "proxy_pass" not in block
        assert "proxy_cache" not in block


def test_acme_bypasses_under_attack_mode_in_every_server_block():
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = render_site(
        _under_attack_site(), blitzecdn_nginx_under_attack_enabled=True
    )
    expected = len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])

    assert rendered.count("location ^~ /.well-known/acme-challenge/ {") == expected
    acme_blocks = rendered.split("location ^~ /.well-known/acme-challenge/ {")[1:]
    for block in acme_blocks:
        location = block.split("    }", 1)[0]
        assert "auth_request" not in location
        assert "js_content" not in location
        assert "allow " not in location
        assert "deny " not in location


def test_missing_always_use_https_preserves_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role."""
    site = site_to_ansible(
        site_from_settings(
            {
                "name": "pre-upgrade",
                "server_names": ["pre-upgrade.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        )
    )
    del site["always_use_https"]

    option = role_spec()["blitzecdn_nginx_sites"]["options"]["always_use_https"]
    assert option["default"] is False
    assert option.get("required", False) is False

    rendered = render_site(site)
    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2


def test_every_virtual_host_exposes_its_release_identity_without_an_origin():
    digest = "a" * 64
    rendered = render_site(
        site_to_ansible(mode_site("flexible", True)), blitzecdn_release_digest=digest
    )
    http, https = server_blocks(rendered, role_defaults())
    for block in (*http, *https):
        assert "location = /.blitzecdn/release" in block
        assert f'return 200 "{digest}\\n";' in block
