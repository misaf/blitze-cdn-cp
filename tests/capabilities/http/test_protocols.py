"""The baseline HTTP capability: its policy contract and its listener stance.

The scheme, port sets, the `http3_enabled` switch and the baseline written
into the fleet document. HTTP/3's derivation is tested in `http/http3`.
"""

from support.policy import TEST_CERTIFICATE_VERSION

from blitzecdn.capabilities.http.contract import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
    ProtocolPolicy,
)
from blitzecdn.capabilities.http.feature import FEATURES
from blitzecdn.capabilities.http.wiring import baseline_listeners
from blitzecdn.capabilities.tls.contract import managed_certificate_paths
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.site_policy import SitePolicy

(FEATURE,) = FEATURES


def _site(name: str, *, enabled: bool = True, http3: bool = True) -> CdnSite:
    chain, key = managed_certificate_paths(name, TEST_CERTIFICATE_VERSION)
    return CdnSite(
        name=name,
        server_names=(f"{name}.example.com",),
        origin_host="198.51.100.10",
        policy=SitePolicy.from_settings(
            {
                "enabled": enabled,
                "ssl_mode": "full",
                "certificate_mode": "requested",
                "certificate_path": chain,
                "certificate_key_path": key,
                "http3_enabled": http3,
            }
        ),
    )


def test_http_owns_the_scheme_and_the_public_proxy_port_sets():
    assert HttpScheme.__module__ == "blitzecdn.capabilities.http.contract"
    assert DEFAULT_PORTS == {HttpScheme.HTTP: 80, HttpScheme.HTTPS: 443}
    assert 80 in HTTP_PROXY_PORTS and 443 in HTTPS_PROXY_PORTS
    assert not set(HTTP_PROXY_PORTS) & set(HTTPS_PROXY_PORTS)


def test_baseline_http_is_a_required_capability_and_provides_no_optional_token():
    """HTTP/1.1 and HTTP/2 are not detachable, so `http` ships with the core."""
    metadata = FEATURE.metadata

    assert metadata.name == "http"
    assert metadata.provides == frozenset()
    assert "http3" not in metadata.capabilities


def test_serving_a_site_over_http1_and_http2_needs_nothing_installed():
    assert ProtocolPolicy().required_capabilities == frozenset()


def test_the_stable_contract_still_carries_the_http3_switch():
    """The field is core's, so a stored site asking for HTTP/3 always loads.

    That makes "requested but not installed" a validation error rather than an
    unreadable row.
    """
    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert ProtocolPolicy(http3_enabled=True).required_capabilities == frozenset(
        {"http3"}
    )


def test_baseline_http_writes_the_off_listener_state_whatever_the_fleet_wants():
    """Constant, not derived: the derivation belongs to the HTTP/3 provider.

    Both variables are required by the edge role, so the baseline writes them and
    the provider replaces them through `overrides`.
    """
    contribution = baseline_listeners(
        (_site("bravo"), _site("alpha"), _site("disabled", enabled=False))
    )

    assert contribution.feature == "http"
    assert contribution.overrides == frozenset()
    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_a_registry_without_http3_refuses_a_site_that_requests_it():
    """A registry without HTTP/3 refuses a site that requests it."""
    from blitzecdn.composition import BUILTIN_FEATURES
    from blitzecdn.core.features import FeatureRegistry, load_features

    builtins = FeatureRegistry(
        load_features(
            tuple(
                path
                for path in BUILTIN_FEATURES
                if path != "blitzecdn.capabilities.http.http3.feature"
            )
        )
    )

    assert "http3" not in builtins.capabilities
    assert "http3" not in _site("alpha", http3=False).policy.required_capabilities
    assert "http3" in builtins.missing(_site("alpha").policy.required_capabilities)
