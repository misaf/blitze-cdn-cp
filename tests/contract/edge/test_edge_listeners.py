"""Public listeners, the default server, and HTTP/3 on UDP/443."""

from __future__ import annotations

from typing import Any

import yaml

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from contract_support import (
    _role_defaults,
)
from edge_render_support import (
    _contract,
    _defaults_of,
    _mode_site,
    _nginx_environment,
    _nginx_resources,
    _render,
    _role,
    _seed_site,
)

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    Domain,
)
from blitzecdn.capabilities.http.policy import (
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
)
from blitzecdn.capabilities.tls.policy import (
    SslMode,
)
from blitzecdn.composition import ControlPlane, Repository

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

    Both roles read one contract member now, so the two cannot fall out of
    lockstep — what is still worth pinning is that the member holds the
    Cloudflare-compatible proxy port sets, and that the domain's copy agrees.
    """
    http_ports = [80, 8080, 8880, 2052, 2082, 2086, 2095]
    https_ports = [443, 2053, 2083, 2087, 2096, 8443]

    assert _contract("listeners", "http") == http_ports
    assert _contract("listeners", "https") == https_ports
    assert _contract("listeners", "http3") is False
    # The domain holds the second copy, because Flexible's origin scheme now
    # depends on which set a listener belongs to. It is the last one: the Nginx
    # role binds these ports and the firewall opens them from the same contract
    # member, so a listener without a rule is no longer possible to write.
    assert list(HTTP_PROXY_PORTS) == http_ports
    assert list(HTTPS_PROXY_PORTS) == https_ports
    firewall = _defaults_of(_role("blitzecdn_firewall"))
    assert "blitzecdn_firewall_http_ports" not in firewall
    assert "blitzecdn_firewall_http3_enabled" not in firewall


def test_default_server_claims_every_public_listener():
    """Unknown hostnames must not fall through to a customer site on any port."""
    environment = _nginx_environment()
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = environment.get_template("default.conf.j2").render(**defaults)

    for port in runtime["listeners"]["http"]:
        assert f"listen {port} default_server;" in rendered
        assert f"listen [::]:{port} default_server;" in rendered
    for port in runtime["listeners"]["https"]:
        assert f"listen {port} ssl default_server;" in rendered
        assert f"listen [::]:{port} ssl default_server;" in rendered


def _http3_site(name: str = "quic") -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
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
    rendered = _render(_http3_site())

    assert "listen 443 ssl;" in rendered
    assert "http2 on;" in rendered
    assert "listen 443 quic;" in rendered
    assert "listen [::]:443 quic;" in rendered
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in rendered
    for port in (2053, 2083, 2087, 2096, 8443):
        assert f"listen {port} quic" not in rendered
        assert f"listen [::]:{port} quic" not in rendered


def test_http3_alt_svc_is_in_the_proxy_location_header_set():
    rendered = _render(_http3_site())
    assert rendered.count("add_header Alt-Svc") == 1
    assert "add_header Alt-Svc 'h3=\":443\"; ma=86400' always;" in rendered
    location = rendered[rendered.index("location / {") :]
    assert "add_header Alt-Svc" in location


def test_http3_disabled_emits_neither_quic_nor_alt_svc():
    site = _http3_site()
    site["http3_enabled"] = False
    rendered = _render(site)
    assert "listen 443 quic" not in rendered
    assert "Alt-Svc" not in rendered


def test_default_server_owns_reuseport_once_for_many_http3_sites():
    defaults = _role_defaults(blitzecdn_edge_http3_enabled=True)
    defaults["blitzecdn_nginx_resources"] = _nginx_resources()
    environment = _nginx_environment()
    catch_all = environment.get_template("default.conf.j2").render(**defaults)
    sites = "".join(_render(_http3_site(name)) for name in ("alpha", "bravo"))

    assert catch_all.count("quic reuseport default_server") == 2
    assert "reuseport" not in sites
    assert sites.count("listen 443 quic;") == 2
    assert "ssl_reject_handshake on;" in catch_all


def test_first_http3_site_owns_reuseport_without_a_catch_all():
    overrides = {
        "blitzecdn_nginx_default_server": False,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
    alpha = _render(_http3_site("alpha"), **overrides)
    bravo = _render(_http3_site("bravo"), **overrides)

    assert alpha.count("quic reuseport;") == 2
    assert "reuseport" not in bravo


def test_desired_state_states_http3_once_for_the_firewall_and_the_listener(
    settings, tmp_path
):
    """The shape of the QUIC contract, which does not depend on what is installed.

    One key, not two. The firewall's UDP/443 rule and the QUIC listener read the
    same contract member, so desired state states HTTP/3 once and the edge play
    no longer has to assert that two copies of it agree.

    Both keys are `required: true` in the edge and Nginx argument specs, so they
    are emitted whether or not `blitzecdn-http3` is attached — core writes the
    baseline and the package overrides it. The *values* are that package's
    behavior and are asserted in its own tests and in the packaging lifecycle;
    what core owns is that these two names, and no others, carry the answer.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    for name in ("zeta", "alpha"):
        _seed_site(
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
    control.deployments.write_desired_state(repository.snapshot(), output)
    document = yaml.safe_load(output.read_text(encoding="utf-8"))

    assert isinstance(document["blitzecdn_edge_http3_enabled"], bool)
    assert isinstance(document["blitzecdn_nginx_http3_listener_owner"], str)
    assert "blitzecdn_nginx_http3_enabled" not in document
    assert "blitzecdn_firewall_http3_enabled" not in document


def test_http3_alt_svc_follows_the_listener_not_the_origin():
    """Alt-Svc advertises the edge's own :443 on the :443 listener only."""
    site = _mode_site(SslMode.FLEXIBLE, serves_tls=True, http3_enabled=True)
    rendered = _render(site_to_ansible(site))
    assert "http://origin.example.com:443" in rendered
    assert rendered.count("add_header Alt-Svc 'h3=\":443\"; ma=86400' always;") == 1
