"""SSL modes, the origin leg, and the upstreams each listener proxies to."""

from __future__ import annotations

import re
from typing import Any

import pytest
from support.contracts import (
    ansible_bool,
    role_defaults,
)
from support.edge_render import (
    ROLE_DIR,
    jinja2,
    mode_site,
    render_site,
    rendered_upstreams,
    server_blocks,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.http.contract import (
    HttpScheme,
)
from blitzecdn.capabilities.origins.contract import OriginPolicy
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    SslMode,
)

REQUIRES_CAPABILITIES = {
    "test_the_cache_key_still_separates_the_listener_ports": ("cache",),
}


def test_site_template_renders_from_real_model_output(desired_state):
    """Catches template breakage that --syntax-check cannot see."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = ansible_bool
    context = role_defaults()
    for site in desired_state["blitzecdn_nginx_sites"]:
        rendered = environment.get_template("site.conf.j2").render(**context, item=site)
        for hostname in site["server_names"]:
            assert f"server_name {hostname};" in rendered
        assert "proxy_pass" in rendered
        if site["certificate_mode"] == CertificateMode.DISABLED:
            assert "ssl_certificate" not in rendered
        else:
            assert f"ssl_certificate {site['certificate_path']};" in rendered


def test_the_template_sends_the_sni_the_control_plane_probed_with():
    """Both halves must resolve SNI identically, and never to a wildcard.

    `OriginProbe` verifies against `OriginPolicy.effective_sni` and the edge
    sends it; if they drift, a preflight pass means nothing. A wildcard
    `server_name` is legal in nginx but unmatchable in a handshake.
    """
    site = site_from_settings(
        {
            "name": "wildcard",
            "server_names": ["*.example.com", "example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": SslMode.FULL_STRICT,
            "certificate_mode": CertificateMode.EXISTING,
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )
    assert (
        site.policy[OriginPolicy].effective_sni(site.origin_host)
        == "origin.example.com"
    )
    rendered = render_site(site_to_ansible(site))
    assert "proxy_ssl_name origin.example.com;" in rendered
    assert "proxy_ssl_verify on;" in rendered
    assert (
        "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;" in rendered
    )
    assert "proxy_ssl_verify_depth 5;" in rendered


#: (mode, serves_tls, origin scheme on an HTTP listener, on the canonical HTTPS
#: listener :443, on an alternate HTTPS listener).
#:
#: The origin port is the listener's own, except Flexible on 443, which reaches
#: the HTTP origin on 80. The scheme follows the visitor (Full and Full (strict)
#: mirror it), and for Flexible also the port: Flexible on 443, Full-like on
#: 2053/2083/2087/2096/8443.
_MODE_SCHEMES = [
    (SslMode.OFF, False, "http", None, None),
    (SslMode.FLEXIBLE, True, "http", "http", "https"),
    (SslMode.FULL, True, "http", "https", "https"),
    (SslMode.FULL_STRICT, True, "http", "https", "https"),
]


def _expected_upstreams(
    defaults: dict[str, Any],
    serves_tls: bool,
    http_origin: str,
    canonical_origin: str | None,
    alternate_origin: str | None,
    host: str = "origin.example.com",
) -> set[str]:
    """Every upstream a site in one mode must emit, keyed by listener."""
    runtime = defaults["blitzecdn_edge_runtime"]
    expected = {
        f"{http_origin}://{host}:{port}" for port in runtime["listeners"]["http"]
    }
    if serves_tls:
        for port in runtime["listeners"]["https"]:
            scheme = canonical_origin if port == 443 else alternate_origin
            origin_port = 80 if port == 443 and scheme == "http" else port
            expected.add(f"{scheme}://{host}:{origin_port}")
    return expected


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_every_ssl_mode_renders_its_transport(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """Every listener proxies over the mode's scheme for it."""
    rendered = render_site(site_to_ansible(mode_site(mode, serves_tls)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    for port in runtime["listeners"]["http"]:
        assert f"listen {port};" in rendered
        assert f"listen [::]:{port};" in rendered
    for port in runtime["listeners"]["https"]:
        assert (f"listen {port} ssl;" in rendered) is serves_tls
        assert (f"listen [::]:{port} ssl;" in rendered) is serves_tls

    assert rendered_upstreams(rendered) == _expected_upstreams(
        defaults, serves_tls, http_origin, canonical_origin, alternate_origin
    )
    assert "return 301 https://$host$request_uri;" not in rendered


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_the_visitor_port_is_preserved_toward_the_origin(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """Origin port == listener port, except Flexible's 443, which uses 80."""
    rendered = render_site(site_to_ansible(mode_site(mode, serves_tls)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    ports = {
        int(upstream.rsplit(":", 1)[1]) for upstream in rendered_upstreams(rendered)
    }
    listeners = set(runtime["listeners"]["http"])
    if serves_tls:
        listeners |= set(runtime["listeners"]["https"])
    if mode is SslMode.FLEXIBLE:
        listeners -= {443}
    assert ports == listeners


#: Representative alternate ports plus the canonical pair. One case per row of
#: the SSL-mode matrix, spelled out as the literal upstream the edge must emit.
_PORT_UPSTREAMS = [
    (SslMode.OFF, False, 80, "http://origin.example.com:80"),
    (SslMode.OFF, False, 8080, "http://origin.example.com:8080"),
    (SslMode.OFF, False, 2052, "http://origin.example.com:2052"),
    (SslMode.FLEXIBLE, True, 8080, "http://origin.example.com:8080"),
    # Flexible is Flexible on 443 (HTTP to the origin's 80) and Full-like on
    # every other HTTPS port.
    (SslMode.FLEXIBLE, True, 443, "http://origin.example.com:80"),
    (SslMode.FLEXIBLE, True, 2053, "https://origin.example.com:2053"),
    (SslMode.FLEXIBLE, True, 2083, "https://origin.example.com:2083"),
    (SslMode.FLEXIBLE, True, 2087, "https://origin.example.com:2087"),
    (SslMode.FLEXIBLE, True, 2096, "https://origin.example.com:2096"),
    (SslMode.FLEXIBLE, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL, True, 80, "http://origin.example.com:80"),
    (SslMode.FULL, True, 8080, "http://origin.example.com:8080"),
    (SslMode.FULL, True, 443, "https://origin.example.com:443"),
    (SslMode.FULL, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL_STRICT, True, 2052, "http://origin.example.com:2052"),
    (SslMode.FULL_STRICT, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL_STRICT, True, 2053, "https://origin.example.com:2053"),
]


@pytest.mark.parametrize(("mode", "serves_tls", "port", "upstream"), _PORT_UPSTREAMS)
def test_representative_ports_render_their_own_upstream(
    mode, serves_tls, port, upstream
):
    rendered = render_site(site_to_ansible(mode_site(mode, serves_tls)))
    assert upstream in rendered_upstreams(rendered)


def test_flexible_443_is_plaintext_and_flexible_8443_is_not():
    """Flexible applies on 443 only; the other HTTPS proxy ports fall back to Full."""
    upstreams = rendered_upstreams(
        render_site(site_to_ansible(mode_site(SslMode.FLEXIBLE, True)))
    )

    assert "http://origin.example.com:80" in upstreams
    assert "http://origin.example.com:443" not in upstreams
    assert "https://origin.example.com:443" not in upstreams
    assert "https://origin.example.com:8443" in upstreams
    assert "http://origin.example.com:8443" not in upstreams


def _https_block(rendered: str, defaults: dict[str, Any], port: int) -> str:
    """The one HTTPS server block listening on ``port``."""
    _, https_blocks = server_blocks(rendered, defaults)
    matching = [block for block in https_blocks if f"listen {port} ssl;" in block]
    assert len(matching) == 1, f"expected exactly one :{port} block"
    return matching[0]


def test_flexible_emits_origin_tls_only_on_the_fallback_listeners():
    """443 terminates and re-originates plaintext; the alternates do not.

    So proxy_ssl_* directives are attributed to the listener, not the site.
    """
    rendered = render_site(site_to_ansible(mode_site(SslMode.FLEXIBLE, True)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, _ = server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_" not in block
    assert "proxy_ssl_" not in _https_block(rendered, defaults, 443)

    for port in runtime["listeners"]["https"]:
        if port == 443:
            continue
        block = _https_block(rendered, defaults, port)
        # Full-like transport and SNI, but never Full (strict)'s verification.
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_name origin.example.com;" in block
        assert "proxy_ssl_verify off;" in block
        assert "proxy_ssl_trusted_certificate" not in block


def test_full_strict_verifies_on_every_https_listener_including_alternates():
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL_STRICT, True)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    for port in runtime["listeners"]["https"]:
        block = _https_block(rendered, defaults, port)
        assert f"https://origin.example.com:{port}" in block
        assert "proxy_ssl_verify on;" in block
        assert "proxy_ssl_server_name on;" in block


def test_ssl_off_never_encrypts_an_origin_leg_on_any_port():
    rendered = render_site(site_to_ansible(mode_site(SslMode.OFF, serves_tls=False)))
    assert "proxy_ssl_" not in rendered
    assert "https://origin.example.com" not in rendered


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_the_template_agrees_with_the_domains_scheme_rule(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """The rule is written twice (Jinja and Python), so pin them together.

    ``site.conf.j2`` cannot import ``SslMode``, so the rendered upstream set is
    compared for equality with the domain method for every mode and listener,
    port included.
    """
    rendered = render_site(site_to_ansible(mode_site(mode, serves_tls)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    listeners = [(HttpScheme.HTTP, port) for port in runtime["listeners"]["http"]]
    if serves_tls:
        listeners += [
            (HttpScheme.HTTPS, port) for port in runtime["listeners"]["https"]
        ]
    assert rendered_upstreams(rendered) == {
        f"{mode.origin_scheme_for(visitor_scheme, port).value}"
        f"://origin.example.com:{mode.origin_port_for(visitor_scheme, port)}"
        for visitor_scheme, port in listeners
    }


def test_full_strict_verifies_only_where_the_origin_leg_is_https():
    """TLS directives belong to the HTTPS listeners and nowhere else.

    Under Full (strict) an HTTP visitor is proxied over HTTP, so its location
    carries no proxy_ssl_* directive.
    """
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL_STRICT, True)))
    defaults = role_defaults()
    http_blocks, https_blocks = server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_verify" not in block
        assert "proxy_ssl_server_name" not in block
        assert "proxy_ssl_name" not in block
        assert "proxy_ssl_trusted_certificate" not in block
    for block in https_blocks:
        assert "proxy_ssl_verify on;" in block
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_name origin.example.com;" in block
        assert (
            "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;" in block
        )
        assert "proxy_ssl_verify_depth 5;" in block


def test_full_does_not_verify_and_still_sends_sni_on_https_listeners():
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, True)))
    defaults = role_defaults()
    http_blocks, https_blocks = server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_" not in block
    for block in https_blocks:
        assert "proxy_ssl_verify off;" in block
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_trusted_certificate" not in block


def test_an_ipv6_origin_is_bracketed_on_every_listener_port():
    """The literal has to keep its brackets once a port is appended to it."""
    site = mode_site(SslMode.FULL, serves_tls=True, origin_host="2001:db8::10")
    rendered = render_site(site_to_ansible(site))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    assert rendered_upstreams(rendered) == {
        f"http://[2001:db8::10]:{port}" for port in runtime["listeners"]["http"]
    } | {f"https://[2001:db8::10]:{port}" for port in runtime["listeners"]["https"]}
    # No unbracketed form anywhere, which would parse as host 2001 port db8.
    assert "//2001:db8::10:" not in rendered


def test_a_pinned_resolver_free_edge_still_preserves_the_listener_port():
    """Without resolvers the upstream is a literal proxy_pass, same rule."""
    rendered = render_site(
        site_to_ansible(mode_site(SslMode.FULL, serves_tls=True)),
        blitzecdn_nginx_resolvers=[],
    )
    assert "set $blitzecdn_upstream" not in rendered
    passes = set(re.findall(r"proxy_pass (\S+);", rendered))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    assert passes == {
        f"http://origin.example.com:{port}" for port in runtime["listeners"]["http"]
    } | {f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]}


def test_the_origin_request_host_override_survives_port_preservation():
    site = mode_site(SslMode.FULL, serves_tls=True, origin_request_host="app.internal")
    rendered = render_site(site_to_ansible(site))
    assert "proxy_set_header Host app.internal;" in rendered
    assert "http://origin.example.com:8080" in rendered
    assert "https://origin.example.com:8443" in rendered


def test_the_cache_key_still_separates_the_listener_ports():
    """$server_port keeps :8080 and :80 from sharing a cached object.

    Two listeners may reach genuinely different origin services.
    """
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, True)))
    assert rendered.count('proxy_cache_key "$scheme$server_port$request_method') >= 2


def test_the_acme_challenge_path_never_proxies_on_any_listener():
    """Challenge files are served from disk, so no listener turns one into an
    origin request on its own port."""
    rendered = render_site(site_to_ansible(mode_site(SslMode.FULL, True)))
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    expected = len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])
    assert rendered.count("location ^~ /.well-known/acme-challenge/ {") == expected
    for block in re.findall(
        r"location \^~ /\.well-known/acme-challenge/ \{(.*?)\n    \}",
        rendered,
        re.S,
    ):
        assert "proxy_pass" not in block
        assert f"root {runtime['paths']['acme']};" in block
