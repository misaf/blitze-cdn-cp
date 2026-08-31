"""The HTTP capability: its policy contract and the fleet listener state."""

from blitzecdn.features.http.plugin import blitzecdn_fleet_desired_state
from blitzecdn.features.http.policy import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
)
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.tls.policy import managed_certificate_paths


def _site(name: str, *, enabled: bool = True, http3: bool = True) -> CdnSite:
    chain, key = managed_certificate_paths(name)
    return CdnSite(
        name=name,
        server_names=(f"{name}.example.com",),
        origin_host="198.51.100.10",
        enabled=enabled,
        ssl_mode="full",
        certificate_mode="requested",
        certificate_path=chain,
        certificate_key_path=key,
        http3_enabled=http3,
    )


def test_http_owns_the_scheme_and_the_public_proxy_port_sets():
    assert HttpScheme.__module__ == "blitzecdn.features.http.policy"
    assert DEFAULT_PORTS == {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}
    assert 80 in HTTP_PROXY_PORTS and 443 in HTTPS_PROXY_PORTS
    assert not set(HTTP_PROXY_PORTS) & set(HTTPS_PROXY_PORTS)


def test_http_projects_the_quic_fleet_requirement_and_exactly_one_owner():
    contribution = blitzecdn_fleet_desired_state(
        (_site("bravo"), _site("alpha"), _site("disabled", enabled=False)), object()
    )

    assert contribution.plugin == "http"
    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }


def test_a_fleet_with_no_http3_site_opens_no_quic_listener():
    contribution = blitzecdn_fleet_desired_state(
        (_site("alpha", http3=False),), object()
    )

    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }
