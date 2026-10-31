"""Public listeners, the default server, and HTTP/3 on UDP/443."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from support.contracts import (
    role_defaults,
)
from support.control_plane import build_application
from support.edge_render import (
    defaults_of,
    mode_site,
    nginx_environment,
    nginx_resources,
    render_site,
    role_path,
    runtime_contract,
    seed_site,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.http.contract import (
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
)
from blitzecdn.capabilities.tls.contract import (
    SslMode,
)
from blitzecdn.composition import Repository

REQUIRES_CAPABILITIES = {
    "test_http3_is_additive_and_limited_to_udp_443": ("http3",),
    "test_http3_alt_svc_is_in_the_proxy_location_header_set": ("http3",),
    "test_http3_alt_svc_follows_the_listener_not_the_origin": ("http3",),
    "test_default_server_owns_reuseport_once_for_many_http3_sites": ("http3",),
    "test_first_http3_site_owns_reuseport_without_a_catch_all": ("http3",),
}


def test_public_ports_match_cloudflare_and_the_firewall():
    """A listener without a firewall rule is unreachable, and the reverse
    exposes a port that can never serve traffic.

    Both roles read one contract member; this pins that it holds the
    Cloudflare-compatible port sets and that the domain's copy agrees.
    """
    http_ports = [80, 8080, 8880, 2052, 2082, 2086, 2095]
    https_ports = [443, 2053, 2083, 2087, 2096, 8443]

    assert runtime_contract("listeners", "http") == http_ports
    assert runtime_contract("listeners", "https") == https_ports
    assert runtime_contract("listeners", "http3") is False
    # The domain holds the second copy, because Flexible's origin scheme depends on
    # which set a listener belongs to.
    assert list(HTTP_PROXY_PORTS) == http_ports
    assert list(HTTPS_PROXY_PORTS) == https_ports
    firewall = defaults_of(role_path("blitzecdn_firewall"))
    assert "blitzecdn_firewall_http_ports" not in firewall
    assert "blitzecdn_firewall_http3_enabled" not in firewall


def test_default_server_claims_every_public_listener():
    """Unknown hostnames must not fall through to a customer site on any port."""
    environment = nginx_environment()
    defaults = role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = environment.get_template("default.conf.j2").render(**defaults)

    for port in runtime["listeners"]["http"]:
        assert f"listen {port} default_server backlog=16384;" in rendered
        assert f"listen [::]:{port} default_server backlog=16384;" in rendered
    for port in runtime["listeners"]["https"]:
        assert f"listen {port} ssl default_server backlog=16384;" in rendered
        assert f"listen [::]:{port} ssl default_server backlog=16384;" in rendered


def _http3_site(name: str = "quic") -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": name,
                "server_names": [f"{name}.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "http3_enabled": True,
                "minimum_tls_version": "1.2",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        )
    )


def test_http3_is_additive_and_limited_to_udp_443():
    rendered = render_site(_http3_site())

    assert "listen 443 ssl;" in rendered
    assert "http2 on;" in rendered
    assert "listen 443 quic;" in rendered
    assert "listen [::]:443 quic;" in rendered
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in rendered
    for port in (2053, 2083, 2087, 2096, 8443):
        assert f"listen {port} quic" not in rendered
        assert f"listen [::]:{port} quic" not in rendered


def test_http3_alt_svc_is_in_the_proxy_location_header_set():
    rendered = render_site(_http3_site())
    assert rendered.count("add_header Alt-Svc") == 1
    assert "add_header Alt-Svc 'h3=\":443\"; ma=86400' always;" in rendered
    location = rendered[rendered.index("location / {") :]
    assert "add_header Alt-Svc" in location


def test_http3_disabled_emits_neither_quic_nor_alt_svc():
    site = _http3_site()
    site["http3_enabled"] = False
    rendered = render_site(site)
    assert "listen 443 quic" not in rendered
    assert "Alt-Svc" not in rendered


def test_default_server_owns_reuseport_once_for_many_http3_sites():
    defaults = role_defaults(blitzecdn_edge_http3_enabled=True)
    defaults["blitzecdn_nginx_resources"] = nginx_resources()
    environment = nginx_environment()
    catch_all = environment.get_template("default.conf.j2").render(**defaults)
    sites = "".join(render_site(_http3_site(name)) for name in ("alpha", "bravo"))

    assert catch_all.count("quic reuseport default_server") == 2
    assert "reuseport" not in sites
    assert sites.count("listen 443 quic;") == 2
    assert "ssl_reject_handshake on;" in catch_all


def test_first_http3_site_owns_reuseport_without_a_catch_all():
    overrides = {
        "blitzecdn_nginx_default_server": False,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
    alpha = render_site(_http3_site("alpha"), **overrides)
    bravo = render_site(_http3_site("bravo"), **overrides)

    assert alpha.count("quic reuseport;") == 2
    assert "reuseport" not in bravo


def test_desired_state_states_http3_once_for_the_firewall_and_the_listener(
    settings, tmp_path
):
    """The shape of the QUIC contract, which does not depend on what is installed.

    The firewall's UDP/443 rule and the QUIC listener read one member. Both keys
    are required by the edge and Nginx argument specs, so core writes the
    baseline and the HTTP/3 provider overrides the values, tested there.
    """
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    for name in ("zeta", "alpha"):
        seed_site(
            repository,
            name=f"{name}-example-com",
            label=name,
            origin="198.51.100.20",
            ssl_mode="flexible",
            http3_enabled=True,
            certificate_mode="existing",
            certificate_path="/etc/ssl/certs/edge.pem",
            certificate_key_path="/etc/ssl/private/edge.key",
        )
    output = tmp_path / "http3.yml"
    control.deployments.publish_artifact(control.releases.compile(), output)
    document = yaml.safe_load(output.read_text(encoding="utf-8"))

    assert isinstance(document["blitzecdn_edge_http3_enabled"], bool)
    assert isinstance(document["blitzecdn_nginx_http3_listener_owner"], str)
    assert "blitzecdn_nginx_http3_enabled" not in document
    assert "blitzecdn_firewall_http3_enabled" not in document


def test_http3_alt_svc_follows_the_listener_not_the_origin():
    """Alt-Svc advertises the edge's own :443 on the :443 listener only."""
    site = mode_site(SslMode.FLEXIBLE, serves_tls=True, http3_enabled=True)
    rendered = render_site(site_to_ansible(site))
    assert "http://origin.example.com:80" in rendered
    assert rendered.count("add_header Alt-Svc 'h3=\":443\"; ma=86400' always;") == 1


@pytest.mark.parametrize("default_server", [False, "false"])
def test_backlog_has_one_owner_per_socket_without_the_default_server(default_server):
    sites = [_http3_site("disabled"), _http3_site("plain"), _http3_site("secure")]
    sites[0]["enabled"] = False
    sites[1]["ssl_mode"] = "off"
    sites[2]["server_names"].append("second.example.com")
    defaults = role_defaults()
    defaults.update(
        blitzecdn_nginx_default_server=default_server,
        blitzecdn_nginx_sites=sites,
        blitzecdn_nginx_listen_backlog=4096,
    )
    environment = nginx_environment()
    rendered = "\n".join(
        environment.get_template("site.conf.j2").render(item=site, **defaults)
        for site in sites
        if site["enabled"]
    )
    for port in defaults["blitzecdn_edge_runtime"]["listeners"]["http"]:
        assert rendered.count(f"listen {port} backlog=4096;") == 1
        assert rendered.count(f"listen [::]:{port} backlog=4096;") == 1
    for port in defaults["blitzecdn_edge_runtime"]["listeners"]["https"]:
        assert rendered.count(f"listen {port} ssl backlog=4096;") == 1
        assert rendered.count(f"listen [::]:{port} ssl backlog=4096;") == 1
    assert "quic backlog" not in rendered
