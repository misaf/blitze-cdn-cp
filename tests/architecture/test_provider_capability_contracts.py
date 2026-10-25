"""Package capability contracts boundaries."""

from __future__ import annotations

import ast

from paths import SOURCE

# --- HTTP/3 is optional; the protocol it is a version of is not -------------


#: The two fleet variables that describe the edge's QUIC listener. Core writes
#: them at their baseline and `blitzecdn-http3` overrides them, so both names
#: appear in both distributions — what must not appear in core is the
#: *derivation*, which is what the tests below actually look for.
_QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


def test_the_http_capability_contributes_the_quic_baseline_without_deriving_it():
    """Core may state the baseline; it may not work out who owns `reuseport`.

    The derivation is `site.http3_enabled` read across the fleet, and it now
    lives in the HTTP/3 provider. The `http` feature still writes both
    variables — they are `required: true` in the edge role's argument spec, so
    the document keeps one shape whatever is installed — but it writes them as
    constants. `contract.py` still reads the switch, which is right: the *field*
    is core's and `required_capabilities` is how a site asks for the capability.
    What may not come back is a read in `wiring.py`, because that is the
    derivation, and two features deriving these two variables from one fleet is
    a merge conflict at deploy time rather than a design anybody chose.
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
    """The other direction: the field may not follow the implementation out.

    `CdnSite` contains a `SitePolicy` with a typed protocol value. Persisted
    policy JSON and release inputs are nested; delivery projects flat settings.
    The contract stays available regardless of which runtime package is installed.
    Moving `http3_enabled` into the package would make
    that shape depend on what is installed, and a stored site asking for HTTP/3
    would stop loading on a controller that had detached it.
    """
    from blitzecdn.capabilities.http.contract import ProtocolPolicy
    from blitzecdn.capabilities.serving.domain import CdnSite

    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert "http3_enabled" in CdnSite.model_fields["policy"].annotation.setting_fields()
    assert ProtocolPolicy.__module__.startswith("blitzecdn.capabilities.http")


def test_the_site_contract_keeps_every_country_setting_in_core():
    """The fields may not follow the implementation out.

    `CdnSite` contains them through named fields on `SitePolicy`. Canonical
    storage is nested and delivery projects the flat operator settings.
    Moving one into the package would make that shape depend on what is
    installed, and a stored site asking for a country would stop loading on a
    controller that had detached it.
    """
    from blitzecdn.capabilities.security.contract import SiteFirewall
    from blitzecdn.capabilities.serving.contract.headers import SiteVisitorHeaders
    from blitzecdn.capabilities.serving.domain import CdnSite

    assert {"allowed_countries", "denied_countries"} <= set(SiteFirewall.model_fields)
    assert "ip_country" in SiteVisitorHeaders.model_fields
    assert {"firewall", "visitor_headers"} <= set(
        CdnSite.model_fields["policy"].annotation.setting_fields()
    )
    assert SiteFirewall.__module__.startswith("blitzecdn.capabilities.security")
    assert SiteVisitorHeaders.__module__.startswith("blitzecdn.capabilities.serving")


#: The two contracts allowed to name the `geoip` token, because they are the
#: two that ask for the lookup: a country firewall rule needs a country to
#: compare against, and so does the `BZ-IPCountry` header.
#:
#: It was one file — `sites/domain.py` — which is the narrower whitelist and
#: the worse rule. `sites` named the token on both contracts' behalf, so the
#: composition was where a third country-aware setting would have been
#: registered, and neither contract said anywhere that it needed a lookup.
_GEOIP_AWARE_CONTRACTS = {
    "capabilities/security/contract.py",
    "capabilities/serving/contract/headers.py",
}


def test_the_country_settings_derive_their_token_generically_in_core():
    """The derivation is core's, and it is not written as a GeoIP special case.

    `capability_requirements` maps every stable setting onto the token it needs
    the same way, so `geoip` arrives by the same path as `compression` or
    `http3` — declared by the contract that wants it, merged by `dns` with no
    branch that knows the name. What this refuses is the shape the acceptance
    criteria warn about: a `registry.require("geoip")` sprinkled through
    unrelated services.
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


#: The kinds of work a journal entry can record, and who names each. Every one
#: of these was a member of `WorkflowKind` inside the journal itself, which
#: made a capability's work part of the vocabulary of the capability that
#: merely records it — `certificate` most plainly, since it is an optional
#: wheel's and a controller without that wheel still carried the name.
_WORKFLOW_KINDS_OWNED_BY_A_CAPABILITY = {
    "deployment": "deployments",
    "rollback": "deployments",
    "certificate": "blitzecdn-certificates",
}


def test_the_journal_names_no_capability_s_work():
    """`workflows` records that something reached a checkpoint, never what.

    The mirror of `test_the_composition_names_no_capability_token_at_all`, one
    layer down: `dns` composes every contract's requirements without naming
    one, and `workflows` records every capability's long operations without
    naming one either. `WorkflowKind` is a validated shape now, each kind is
    declared beside the work it names, and what the database checks is that
    shape rather than a list it cannot be told about.
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
    """The property the whitelist above is only the GeoIP half of.

    `dns/domain/` composes every contract's requirements and may not name one
    of them. It named six, in an `if` chain that restated each capability's own
    rule beside it — two places to edit, and nothing to catch the day they
    disagreed.

    Held over the package rather than over `host.py` alone: `patch.py` mirrors
    every field the composition carries, so a token could be reintroduced there
    just as easily.
    """
    tokens = ("geoip", "cache", "compression", "http3", "certificates", "security")
    offenders = [
        f"dns/domain/{path.name} names the {node.value} token"
        for path in sorted((SOURCE / "capabilities/dns/domain").glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value in tokens
    ]
    assert offenders == []
