"""Always Use HTTPS, and Under Attack Mode, across every listener."""

from __future__ import annotations

from typing import Any

import pytest

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from contract_support import (
    _role_defaults,
)
from edge_render_support import (
    ROLE_DIR,
    _mode_site,
    _render,
    _role_spec,
    _server_blocks,
    _upstreams,
)
from paths import REPO_ROOT

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
)
from blitzecdn.capabilities.tls.policy import (
    SslMode,
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
    site = CdnSite.model_validate(
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

    rendered = _render(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" in rendered


def test_always_use_https_uses_a_permanent_redirect():
    """Cloudflare-compatible status code: 301, not 308.

    308 preserves the request method, which is the safer redirect in general and
    the wrong one here — Cloudflare answers Always Use HTTPS with a permanent
    301, and a client that follows it differently from a Cloudflare-fronted
    origin is a compatibility difference the operator cannot see. Every path
    that redirects — the plain HTTP location, both Under Attack Mode challenge
    endpoints, and the named redirect location — uses the same code, so the
    template is checked whole rather than one rendering at a time.
    """
    template = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    security_template = (
        REPO_ROOT
        / "packages/blitzecdn-security/src/blitzecdn_security/nginx"
        / "security-server.conf.j2"
    ).read_text(encoding="utf-8")
    implementation = template + security_template

    assert "return 30" in implementation
    assert "return 308" not in implementation
    assert implementation.count("return 301 https://$host$request_uri;") == 3


def test_always_use_https_redirects_every_http_listener_without_its_port():
    """Redirect semantics are unchanged by port preservation, deliberately.

    The thirteen proxy ports are two independent sets, not seven pairs: 8080's
    counterpart is not 8443, and Cloudflare publishes no mapping between them.
    Carrying an HTTP-only port such as 8080 or 2052 into the Location would
    invent one and send the visitor to an endpoint the edge does not serve, so
    the redirect stays scheme-only. ``$host`` carries no port, which is what
    makes every HTTP listener land on the default 443 — a listener the site
    always serves once it serves TLS at all, so the redirect cannot loop.
    """
    site = _mode_site(SslMode.FULL, serves_tls=True, always_use_https=True)
    rendered = _render(site_to_ansible(site))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

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
    assert _upstreams(rendered) == {
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
    """The second rule written twice, pinned the same way as the first.

    ``always_use_https`` is inert unless the site serves TLS, and the template
    gates on ``tls and always_use_https`` while the control plane answers
    ``CdnSite.redirects_http_to_https``. Assert the rendering against the
    property for every mode, in both the normal and the Under Attack Mode
    request flow, so neither copy can move alone.
    """
    site = _mode_site(
        mode,
        serves_tls=mode is not SslMode.OFF,
        always_use_https=True,
        under_attack_mode=under_attack,
    )
    rendered = _render(
        site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=under_attack
    )

    assert _redirects(rendered) is site.redirects_http_to_https


def test_ssl_off_ignores_always_use_https_instead_of_looping():
    """Off serves no HTTPS listener, so the redirect must not be emitted.

    Cloudflare removes the Always Use HTTPS control from the dashboard while the
    encryption mode is Off. BlitzeCDN keeps the stored preference — the record
    API still accepts the combination, in either order — and renders it inert,
    which is the only outcome that cannot send a visitor to a port the edge does
    not answer on. A permanent 301 to a dead port is worse than a temporary one:
    browsers cache it.
    """
    site = _mode_site(SslMode.OFF, serves_tls=False, always_use_https=True)
    rendered = _render(site_to_ansible(site))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

    assert site.always_use_https is True
    assert site.redirects_http_to_https is False
    assert not https_blocks
    assert "listen 443" not in rendered
    assert not _redirects(rendered)
    # Every HTTP listener still proxies, over HTTP, to its own port.
    assert _upstreams(rendered) == {
        f"http://origin.example.com:{port}" for port in runtime["listeners"]["http"]
    }
    for block in http_blocks:
        assert "proxy_pass" in block


def test_under_attack_mode_redirects_permanently_on_every_challenge_path():
    """The mitigation endpoints redirect with the same code as the proxy path.

    A site in Under Attack Mode with Always Use HTTPS has three HTTP-side
    redirects — the two challenge endpoints and the named location the guarded
    request falls through to — and one of them keeping 308 would answer some
    visitors differently from the rest.
    """
    site = _mode_site(
        SslMode.FULL,
        serves_tls=True,
        always_use_https=True,
        under_attack_mode=True,
    )
    rendered = _render(site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=True)
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, _ = _server_blocks(rendered, defaults)

    assert "return 308" not in rendered
    for block in http_blocks:
        assert "proxy_pass" not in block
        # challenge, verify, and the guarded fall-through, per HTTP listener.
        assert block.count("return 301 https://$host$request_uri;") == 3
    assert _upstreams(rendered) == {
        f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]
    }


def test_always_use_https_can_be_disabled_without_disabling_tls():
    site = CdnSite.model_validate(
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

    rendered = _render(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2
    assert "listen 443 ssl;" in rendered


def _under_attack_site(*, enabled: bool = True) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
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
    rendered = _render(_under_attack_site(enabled=False))

    assert "blitzecdn_under_attack.guard" not in rendered
    assert "X-BlitzeCDN-Mitigation challenge" not in rendered
    assert "auth_request /.blitzecdn/" not in rendered
    assert "proxy_pass" in rendered


def test_under_attack_mode_renders_before_redirect_and_proxy_on_http_and_https():
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)

    assert rendered.count("auth_request /.blitzecdn/internal/under-attack-guard;") == (
        len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])
    )
    assert "try_files /__blitzecdn_dispatch__ @blitzecdn_upstream;" in rendered
    assert "proxy_pass" in rendered
    assert rendered.index("auth_request /.blitzecdn/") < rendered.index("proxy_pass")
    assert "X-BlitzeCDN-Mitigation challenge" in rendered


def test_under_attack_reserved_endpoints_are_edge_only_and_uncached():
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)

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
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)
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
        CdnSite.model_validate(
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

    option = _role_spec()["blitzecdn_nginx_sites"]["options"]["always_use_https"]
    assert option["default"] is False
    assert option.get("required", False) is False

    rendered = _render(site)
    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2
