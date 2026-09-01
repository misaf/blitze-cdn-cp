"""The baseline HTTP capability: its policy contract and its listener stance.

HTTP/3's *derivation* is not here. It moved out with the capability that owns
it — `packages/blitzecdn-http3/tests` — and what stays is the half core keeps
whether or not that distribution is installed: the scheme, the port sets, the
`http3_enabled` switch the stored site contract carries, and the baseline this
plugin writes into the fleet document.
"""

from blitzecdn.core.plugins import load_plugins
from blitzecdn.features.http.plugin import (
    blitzecdn_fleet_desired_state,
    blitzecdn_plugin_metadata,
)
from blitzecdn.features.http.policy import (
    DEFAULT_PORTS,
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
    ProtocolPolicy,
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


def test_baseline_http_is_a_required_capability_and_provides_no_optional_token():
    """HTTP/1.1 and HTTP/2 are not detachable, so `http` ships with the core."""
    metadata = blitzecdn_plugin_metadata()

    assert metadata.name == "http"
    assert metadata.required
    assert metadata.provides == frozenset()
    assert "http3" not in metadata.capabilities


def test_serving_a_site_over_http1_and_http2_needs_nothing_installed():
    assert ProtocolPolicy().required_capabilities == frozenset()


def test_the_stable_contract_still_carries_the_http3_switch():
    """The field is core's, so a stored site asking for HTTP/3 always loads.

    This is what makes "requested but not installed" a validation error the
    control plane can phrase, rather than a row it cannot read back.
    """
    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert ProtocolPolicy(http3_enabled=True).required_capabilities == frozenset(
        {"http3"}
    )


def test_baseline_http_writes_the_off_listener_state_whatever_the_fleet_wants():
    """Constant, and not derived — the derivation belongs to `blitzecdn-http3`.

    Contributed rather than omitted because both variables are `required: true`
    in the edge role's argument spec, so the document has one shape in every
    installation and `blitzecdn-http3` replaces the values through `overrides`.
    """
    contribution = blitzecdn_fleet_desired_state(
        (_site("bravo"), _site("alpha"), _site("disabled", enabled=False)), object()
    )

    assert contribution.plugin == "http"
    assert contribution.overrides == frozenset()
    assert contribution.variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_core_alone_refuses_a_site_that_asks_for_http3():
    """The one intended semantic change, asserted against the built-in set.

    `load_plugins(entry_point_group=None)` is a control plane with no optional
    distribution installed at all, which is what an operator who never attached
    `blitzecdn-http3` is running.
    """
    builtins = load_plugins(entry_point_group=None)

    assert "http3" not in builtins.capabilities
    assert "http3" not in _site("alpha", http3=False).required_capabilities
    assert "http3" in builtins.missing(_site("alpha").required_capabilities)
