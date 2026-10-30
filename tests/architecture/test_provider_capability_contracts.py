"""Package capability contracts boundaries."""

from __future__ import annotations

import ast

from support.paths import SOURCE

# --- HTTP/3 is optional; the protocol it is a version of is not -------------


#: The two fleet variables describing the edge's QUIC listener. Core writes the
#: baseline and the HTTP/3 provider overrides it; what must not appear in core is
#: the derivation.
_QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


def test_the_http_capability_contributes_the_quic_baseline_without_deriving_it():
    """Core may state the baseline; it may not work out who owns `reuseport`.

    The derivation reads `site.http3_enabled` across the fleet and lives in the
    HTTP/3 provider. The `http` feature writes both variables as constants, since
    the edge role requires them. A read in `wiring.py` would be a second
    derivation.
    """
    feature = SOURCE / "capabilities/http/wiring.py"
    offenders = [
        "capabilities/http/wiring.py reads .http3_enabled"
        for node in ast.walk(ast.parse(feature.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "http3_enabled"
    ]
    assert offenders == []

    constants = {
        key.value: getattr(value, "value", value)
        for node in ast.walk(ast.parse(feature.read_text(encoding="utf-8")))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
    }
    assert set(constants) == _QUIC_FLEET_VARIABLES
    assert set(constants.values()) == {False, ""}


def test_the_site_contract_keeps_the_http3_switch_in_core():
    """The field stays in core even though the implementation does not.

    A stored site asking for HTTP/3 must load whatever runtime is installed.
    """
    from blitzecdn.capabilities.http.contract import ProtocolPolicy
    from blitzecdn.core.domain.site import CdnSite

    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert "http3_enabled" in CdnSite.model_fields["policy"].annotation.setting_fields()
    assert ProtocolPolicy.__module__.startswith("blitzecdn.capabilities.http")


def test_the_site_contract_keeps_every_country_setting_in_core():
    """The country fields stay in core even though the implementation does not.

    A stored site asking for a country must load whatever runtime is installed.
    """
    from blitzecdn.capabilities.http.contract import SiteVisitorHeaders
    from blitzecdn.capabilities.security.contract import SiteFirewall
    from blitzecdn.core.domain.site import CdnSite

    assert {"allowed_countries", "denied_countries"} <= set(SiteFirewall.model_fields)
    assert "ip_country" in SiteVisitorHeaders.model_fields
    assert {"firewall", "visitor_headers"} <= set(
        CdnSite.model_fields["policy"].annotation.setting_fields()
    )
    assert SiteFirewall.__module__.startswith("blitzecdn.capabilities.security")
    assert SiteVisitorHeaders.__module__.startswith("blitzecdn.capabilities.http")


#: The two contracts allowed to name the `geoip` token, because they ask for the
#: lookup: a country firewall rule and the `BZ-IPCountry` header.
_GEOIP_AWARE_CONTRACTS = {
    "capabilities/security/contract.py",
    "capabilities/http/contract.py",
}


def test_the_country_settings_derive_their_token_generically_in_core():
    """The derivation is core's, and it is not written as a GeoIP special case.

    `capability_requirements` maps every setting onto its token the same way, so
    `geoip` arrives like `compression` or `http3`: declared by the contract that
    wants it, merged by `dns` with no branch that knows the name.
    """
    offenders = [
        f"{path.relative_to(SOURCE)} names the geoip token"
        for path in sorted(SOURCE.rglob("*.py"))
        if path.relative_to(SOURCE).as_posix() not in _GEOIP_AWARE_CONTRACTS
        and not path.is_relative_to(SOURCE / "capabilities/security/geoip")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "geoip"
    ]
    assert offenders == []


#: The kinds of work a journal entry can record, and the capability that names
#: each.
_WORKFLOW_KINDS_OWNED_BY_A_CAPABILITY = {
    "deployment": "deployments",
    "rollback": "deployments",
    "certificate": "blitzecdn-certificates",
}


def test_the_journal_names_no_capability_s_work():
    """`workflows` records that something reached a checkpoint, never what.

    Each `WorkflowKind` is declared beside the work it names; the journal checks
    the shape only.
    """
    offenders = [
        f"workflows/{path.relative_to(SOURCE / 'capabilities/workflows')} "
        f"names {node.value!r}, which is {owner}'s work"
        for path in sorted((SOURCE / "capabilities/workflows").rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (owner := _WORKFLOW_KINDS_OWNED_BY_A_CAPABILITY.get(node.value))
    ]
    assert offenders == []


def test_the_composition_names_no_capability_token_at_all():
    """`dns/domain/` composes every contract's requirements without naming one.

    Checked over the package, since `patch.py` mirrors every composed field.
    """
    tokens = ("geoip", "cache", "compression", "http3", "certificates", "security")
    offenders = [
        f"dns/domain/{path.name} names the {node.value} token"
        for path in sorted((SOURCE / "capabilities/dns/domain").glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value in tokens
    ]
    assert offenders == []
