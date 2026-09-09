"""Package capability contracts boundaries."""

from __future__ import annotations

import ast

from package_boundary_support import (
    _imports,
    _manifest,
    _next_major_bound,
    _packages,
    _source_files,
)
from paths import SOURCE

from blitzecdn.composition import BUILTIN_PLUGINS, load_control_plane_plugins

# --- HTTP/3 is optional; the protocol it is a version of is not -------------


#: The two fleet variables that describe the edge's QUIC listener. Core writes
#: them at their baseline and `blitzecdn-http3` overrides them, so both names
#: appear in both distributions — what must not appear in core is the
#: *derivation*, which is what the tests below actually look for.
_QUIC_FLEET_VARIABLES = frozenset(
    {"blitzecdn_edge_http3_enabled", "blitzecdn_nginx_http3_listener_owner"}
)


def test_http3_ships_as_an_optional_distribution_and_http1_and_http2_do_not():
    """The asymmetry is the whole design, so it is asserted by name.

    HTTP/1.1 and HTTP/2 are invariants of a managed edge: there is nothing to
    install and nothing to turn on. A `blitzecdn-http1` or `blitzecdn-http2`
    would make ordinary traffic depend on an optional wheel, which is the
    failure this extraction must not drift into.
    """
    distributions = {package.name for package in _packages()}

    assert "blitzecdn-http3" in distributions
    assert not {"blitzecdn-http1", "blitzecdn-http2", "blitzecdn-http"} & distributions
    assert "blitzecdn.capabilities.http.plugin" in BUILTIN_PLUGINS


def test_the_http3_capability_is_reached_only_through_its_entry_point():
    """Attached and detached are both real, and neither touches core.

    The generic tests above already hold this for every package; stated once
    for `http3` too because the token is the one the site contract names, and a
    capability whose token nothing supplied would fail as a validation error on
    every HTTP/3 site rather than as anything obviously packaging-shaped.
    """
    builtins = load_control_plane_plugins(entry_point_group=None)
    installed = load_control_plane_plugins()

    assert "http3" not in builtins.capabilities
    assert "http3" in installed.capabilities
    assert "http3" not in {metadata.name for metadata in builtins.plugins}


def test_the_http_capability_contributes_the_quic_baseline_without_deriving_it():
    """Core may state the baseline; it may not work out who owns `reuseport`.

    The derivation is `site.http3_enabled` read across the fleet, and it now
    lives in `blitzecdn-http3`. Core's `http` plugin still writes both
    variables — they are `required: true` in the edge role's argument spec, so
    the document keeps one shape whatever is installed — but it writes them as
    constants. `policy.py` still reads the switch, which is right: the *field*
    is core's and `required_capabilities` is how a site asks for the capability.
    What may not come back is a read in `plugin.py`, because that is the
    derivation, and two plugins deriving these two variables from one fleet is
    a merge conflict at deploy time rather than a design anybody chose.
    """
    plugin = SOURCE / "capabilities/http/plugin.py"
    offenders = [
        "capabilities/http/plugin.py reads .http3_enabled"
        for node in ast.walk(ast.parse(plugin.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "http3_enabled"
    ]
    assert offenders == []

    constants = {
        key.value: getattr(value, "value", value)
        for node in ast.walk(ast.parse(plugin.read_text(encoding="utf-8")))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
    }
    assert set(constants) == _QUIC_FLEET_VARIABLES
    assert set(constants.values()) == {False, ""}


def test_the_site_contract_keeps_the_http3_switch_in_core():
    """The other direction: the field may not follow the implementation out.

    `CdnSite` composes `ProtocolPolicy` by inheritance and the flat shape is
    what the published schemas, the persisted policy JSON and the deployment
    snapshots all consume. Moving `http3_enabled` into the package would make
    that shape depend on what is installed, and a stored site asking for HTTP/3
    would stop loading on a controller that had detached it.
    """
    from blitzecdn.capabilities.dns.domain import CdnSite
    from blitzecdn.capabilities.http.policy import ProtocolPolicy

    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert "http3_enabled" in CdnSite.model_fields
    assert ProtocolPolicy.__module__.startswith("blitzecdn.capabilities.http")


def test_geoip_ships_as_an_optional_distribution_and_no_consumer_of_it_does():
    """One capability for the lookup, and no package per thing that wants it.

    A `blitzecdn-country-headers` beside a `blitzecdn-country-firewall` would
    be two wheels for one MaxMind database and one Nginx module, and an
    operator would have to know which of them a given site needed.
    """
    distributions = {package.name for package in _packages()}

    assert "blitzecdn-geoip" in distributions
    assert (
        not {
            "blitzecdn-maxmind",
            "blitzecdn-mmdb",
            "blitzecdn-nginx-geoip",
            "blitzecdn-geoip-database",
            "blitzecdn-country",
        }
        & distributions
    )


def test_the_geoip_capability_is_reached_only_through_its_entry_point():
    """Attached and detached are both real, and neither touches core."""
    builtins = load_control_plane_plugins(entry_point_group=None)
    installed = load_control_plane_plugins()

    assert "geoip" not in builtins.capabilities
    assert "geoip" in installed.capabilities
    assert "geoip" not in {metadata.name for metadata in builtins.plugins}
    assert not any("geoip" in module for module in BUILTIN_PLUGINS)


def test_the_site_contract_keeps_every_country_setting_in_core():
    """The fields may not follow the implementation out.

    `CdnSite` composes them by inheritance into the flat shape the published
    schemas, the persisted policy JSON and the deployment snapshots consume.
    Moving one into the package would make that shape depend on what is
    installed, and a stored site asking for a country would stop loading on a
    controller that had detached it.
    """
    from blitzecdn.capabilities.dns.domain import CdnSite
    from blitzecdn.capabilities.dns.policy.headers import SiteVisitorHeaders
    from blitzecdn.capabilities.security.policy import SiteFirewall

    assert {"allowed_countries", "denied_countries"} <= set(SiteFirewall.model_fields)
    assert "ip_country" in SiteVisitorHeaders.model_fields
    assert {"firewall", "visitor_headers"} <= set(CdnSite.model_fields)
    assert SiteFirewall.__module__.startswith("blitzecdn.capabilities.security")
    assert SiteVisitorHeaders.__module__.startswith("blitzecdn.capabilities.dns")


#: The two contracts allowed to name the `geoip` token, because they are the
#: two that ask for the lookup: a country firewall rule needs a country to
#: compare against, and so does the `BZ-IPCountry` header.
#:
#: It was one file — `sites/domain.py` — which is the narrower whitelist and
#: the worse rule. `sites` named the token on both contracts' behalf, so the
#: composition was where a third country-aware setting would have been
#: registered, and neither contract said anywhere that it needed a lookup.
_GEOIP_AWARE_CONTRACTS = {
    "capabilities/security/policy.py",
    "capabilities/dns/policy/headers.py",
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


def test_security_depends_on_the_geoip_token_and_never_on_its_implementation():
    """The country firewall needs a lookup; it may not import the wheel.

    Held over both distributions at once, because the coupling this refuses is
    the tempting one: `blitzecdn-security` owns the rule, `blitzecdn-geoip`
    owns the lookup, and an import between them would make detaching either
    break the other.
    """
    security = next(p for p in _packages() if p.name == "blitzecdn-security")
    geoip = next(p for p in _packages() if p.name == "blitzecdn-geoip")

    pairs = ((security, "blitzecdn_geoip"), (geoip, "blitzecdn_security"))
    for package, forbidden in pairs:
        offenders = [
            f"{path.name} imports {imported}"
            for path in _source_files(package)
            for imported in sorted(_imports(path))
            if imported.split(".")[0] == forbidden
        ]
        assert offenders == []


def test_automatic_ssl_declares_the_origin_probe_it_runs():
    """The workspace's one optional-to-optional edge, held from both sides.

    `blitzecdn-certificates` cannot recommend an SSL upgrade without asking
    every edge whether the origin answers over its current transport and again
    under Full (strict), and that play is `blitzecdn-origins`'. So the edge is
    real and is written down: declared in the manifest with a pinned range, so
    pip installs both and detaching the probe cannot leave the scan importing
    something that is gone.

    Held from both sides because the failure mode is asymmetric. An import
    without the declaration is the silent one — it works on every machine that
    happens to have both — and it is what this pins down.
    """
    certificates = next(p for p in _packages() if p.name == "blitzecdn-certificates")
    requirements = _manifest(certificates)["project"]["dependencies"]

    declared = next(
        (r for r in requirements if r.startswith("blitzecdn-origins")), None
    )
    assert declared is not None, (
        "the Automatic SSL/TLS scan runs blitzecdn-origins' play; declare it "
        "in blitzecdn-certificates' dependencies rather than importing it "
        "opportunistically"
    )
    assert _next_major_bound() in declared

    imports = {
        imported
        for path in _source_files(certificates)
        for imported in _imports(path)
        if imported.split(".")[0] == "blitzecdn_origins"
    }
    assert imports, (
        "blitzecdn-certificates declares blitzecdn-origins and uses none of "
        "it; drop the dependency rather than leaving one nobody needs"
    )
