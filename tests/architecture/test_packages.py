"""Executable boundaries for the optional distributions.

The rule the whole packaging layer rests on is one sentence: **core knows the
extension contracts and never the implementations.** Everything here is a way
of checking that sentence against the real source tree and the real installed
metadata, because every violation of it is invisible in review — an import
added to `bootstrap.py`, a package name that crept into `BUILTIN_PLUGINS`, a
test left behind in `tests/` when its capability moved out — and each one turns
"detach the package" from a supported operation into a broken control plane.

Three layers are held:

* **the source tree** — what names appear where, checked by parsing;
* **the distributions** — what each `pyproject.toml` declares, checked by
  reading the manifests; and
* **the installed environment** — what `importlib.metadata` actually reports,
  which is the path an operator's install takes and the only one that proves
  discovery is really metadata-driven.

`test_lifecycle.py` beside this file takes the last layer further and installs
and uninstalls a real wheel.
"""

from __future__ import annotations

import ast
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from paths import SOURCE, optional_packages

from blitzecdn.core.plugins import (
    BUILTIN_PLUGINS,
    ENTRY_POINT_GROUP,
    build_plugin_manager,
    load_plugins,
    register_builtins,
)


#: The distribution name a workspace member declares, and the import package it
#: ships, are derived from the directory rather than listed: `blitzecdn-cache`
#: ships `blitzecdn_cache`. Adding a package therefore needs no edit here.
def _distribution(package: Path) -> str:
    return package.name


def _import_package(package: Path) -> str:
    return package.name.replace("-", "_")


def _manifest(package: Path) -> dict:
    return tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))


def _source_files(package: Path) -> list[Path]:
    return sorted((package / "src").rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _packages() -> list[Path]:
    found = optional_packages()
    assert found, "the workspace declares packages/* but ships none"
    return found


def _optional_import_roots() -> set[str]:
    return {_import_package(package) for package in _packages()}


# --- core does not know the implementations ---------------------------------


def test_core_never_imports_an_optional_package():
    """The load-bearing rule, checked by name over the whole distribution.

    Not scoped to `bootstrap.py`, because the composition root is only the
    likeliest place for it rather than the only one: a router, a service or a
    `core` module importing `blitzecdn_cache` would make the control plane
    refuse to start the moment that package was uninstalled, which is the
    operation the boundary is for.
    """
    optional = _optional_import_roots()
    offenders = [
        f"{path.relative_to(SOURCE)} imports {imported}"
        for path in sorted(SOURCE.rglob("*.py"))
        for imported in sorted(_imports(path))
        if imported.split(".")[0] in optional
    ]
    assert offenders == []


def test_core_never_branches_on_whether_an_optional_package_is_installed():
    """No `if backup_installed:` — not in core, and not anywhere in the source.

    Asking is as much a dependency as importing. A core module that behaves
    differently when a package happens to be present has an implicit contract
    with that package's *behaviour*, which nothing declares and no test covers.
    Capability availability has exactly one expression — `registry.require`,
    driven by configuration and plugin metadata — and this refuses a second.
    """
    names = {
        f"{token}_installed"
        for root in _optional_import_roots()
        for token in (root.removeprefix("blitzecdn_"),)
    } | {"plugin_installed", "package_installed", "capability_installed"}
    offenders = [
        f"{path.relative_to(SOURCE)} reads {node.id}"
        for path in sorted(SOURCE.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Name) and node.id in names
    ]
    assert offenders == []


def test_no_optional_package_is_also_a_built_in():
    """A capability registers one way or the other, never both.

    Registered twice it would collide on its own name at startup — `register`
    refuses a duplicate whichever side it came from — so this is not a style
    rule. It is the failure that a half-finished extraction produces, and the
    message it produces at runtime blames the entry point rather than the
    leftover line in `BUILTIN_PLUGINS`.
    """
    optional = _optional_import_roots()
    offenders = [
        module
        for module in BUILTIN_PLUGINS
        if module.split(".")[0] in optional
        or any(module.startswith(f"{root}.") for root in optional)
    ]
    assert offenders == []


def test_every_built_in_lives_in_the_control_plane_distribution():
    """The other direction: a built-in is a module of *this* wheel.

    `BUILTIN_PLUGINS` is imported by module path and any failure there is
    fatal, which is right for a capability this distribution ships and wrong
    for one that may not be installed. So the tuple may only name modules under
    `blitzecdn.`, and an optional capability reaches the registry through its
    entry point or not at all.
    """
    assert all(module.startswith("blitzecdn.features.") for module in BUILTIN_PLUGINS)


def test_a_built_in_declares_itself_required_and_an_optional_package_does_not():
    """The failure policy follows the packaging, in both directions.

    A built-in that failed would leave a control plane that is not degraded but
    wrong — a `sites` that did not load renders an empty fleet — so it is fatal
    by definition, and `register_builtins` already refuses a built-in that
    claims otherwise. An optional package's failure is reported by name and
    skipped, and one that declared itself required would take the node down on
    a fault in a capability the operator chose to add.
    """
    manager = build_plugin_manager()
    assert all(metadata.required for metadata in register_builtins(manager))

    installed = load_plugins()
    builtin_names = {
        metadata.name for metadata in load_plugins(entry_point_group=None).plugins
    }
    external = [
        metadata for metadata in installed.plugins if metadata.name not in builtin_names
    ]
    assert external, "no optional distribution is installed in this environment"
    assert not any(metadata.required for metadata in external)


# --- optional packages depend inward ----------------------------------------


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_depends_only_on_the_control_plane(package: Path):
    """One dependency, pointing inward, with an explicit compatibility range.

    The upper bound is not decoration: `HOOK_API_VERSION` may only move in a
    major, and a plugin written against v1 that silently installed beside a v2
    control plane would be refused at registration with a message about a hook
    contract rather than about the dependency that allowed it.
    """
    project = _manifest(package)["project"]
    assert [
        requirement.split(">")[0].split("[")[0].strip()
        for requirement in project["dependencies"]
    ] == ["blitzecdn"]
    assert "<4" in project["dependencies"][0]


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_optional_packages_do_not_depend_on_each_other(package: Path):
    """No optional-to-optional edge, declared or imported.

    Avoided rather than forbidden outright: a package that genuinely needs
    another may declare it as a real dependency in `pyproject.toml`, and pip
    then installs both. What is refused is the *undeclared* form — an import
    that happens to work because both are installed today — because it makes
    detaching one break the other with an ImportError nothing predicted.
    """
    others = _optional_import_roots() - {_import_package(package)}
    offenders = [
        f"{path.name} imports {imported}"
        for path in _source_files(package)
        for imported in sorted(_imports(path))
        if imported.split(".")[0] in others
    ]
    assert offenders == []


#: What an optional package may reach for inside the control plane, by module
#: prefix. Everything here is a *contract*: the plugin SDK, configuration, the
#: shared value types, the ports an installed capability is handed, and the
#: entry-layer toolkits a contributed router or command is built from.
#:
#: The exclusions are the point. `blitzecdn.bootstrap` is the control plane's
#: composition root and a package composes itself; `blitzecdn.core.database*`
#: and `*.persistence` are storage implementations reached through ports;
#: `blitzecdn.api.app` and `blitzecdn.cli.main` are the two application
#: compositions, and a plugin that imported either would be assembling the
#: thing that is assembling it.
_PUBLIC_SDK_PREFIXES = (
    "blitzecdn.core.plugins",
    "blitzecdn.core.config",
    "blitzecdn.core.exceptions",
    "blitzecdn.core.events",
    "blitzecdn.core.operations",
    "blitzecdn.core.operation_ports",
    "blitzecdn.core.runs",
    "blitzecdn.core.schema",
    "blitzecdn.core.validation",
    "blitzecdn.core.filesystem",
    "blitzecdn.core.ports",
    "blitzecdn.core.process",
    "blitzecdn.api.dependencies",
    "blitzecdn.api.operations",
    "blitzecdn.api.requests",
    "blitzecdn.cli.common",
)

#: A capability contract another capability owns. Allowed, and named one by one
#: rather than by a wildcard over `blitzecdn.features.*`: `CdnSite` and
#: `HttpScheme` are contracts every capability already consumes, while a
#: feature's `service` or `adapters` module is not something an installed
#: package may reach into.
_PUBLIC_CAPABILITY_MODULES = (
    "blitzecdn.features.sites",
    "blitzecdn.features.http.policy",
    "blitzecdn.features.dns.ports",
    "blitzecdn.features.deployments.domain",
    "blitzecdn.features.deployments.ports",
    "blitzecdn.features.edges.origins",
    "blitzecdn.features.edges.ports",
    "blitzecdn.features.tls.policy",
)

_FORBIDDEN_SDK_MODULES = (
    "blitzecdn.bootstrap",
    "blitzecdn.worker",
    "blitzecdn.scheduler",
    "blitzecdn.api.app",
    "blitzecdn.cli.main",
    "blitzecdn.core.database",
    "blitzecdn.core.database_engine",
    "blitzecdn.core.database_models",
    "blitzecdn.core.ansible",
    "blitzecdn.core.broker",
)


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_imports_only_public_contracts(package: Path):
    """What a package may name inside the control plane, and what it may not.

    An allowlist rather than a denylist, because the failure this guards is a
    package reaching for something core never meant to publish and nobody
    noticing until core moves it. The list is the public SDK: adding to it is a
    deliberate decision about what BlitzeCDN promises an installed capability.

    `TYPE_CHECKING` imports count. `ControlPlane` is annotated by every package
    that receives one, and that is knowledge of `blitzecdn.bootstrap` whichever
    block it sits in — so it is written as a guarded, annotation-only import
    that this test allows by name, rather than excused by a rule that would
    also let a runtime import through.
    """
    allowed = (*_PUBLIC_SDK_PREFIXES, *_PUBLIC_CAPABILITY_MODULES)
    offenders: list[str] = []
    for path in _source_files(package):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        annotation_only = {
            node
            for branch in ast.walk(tree)
            if isinstance(branch, ast.If)
            and ast.unparse(branch.test) == "TYPE_CHECKING"
            for node in ast.walk(branch)
        }

        def permitted(name: str) -> bool:
            if name in _FORBIDDEN_SDK_MODULES:
                return False
            return any(
                name == prefix or name.startswith(f"{prefix}.") for prefix in allowed
            )

        for node in ast.walk(tree):
            if node in annotation_only:
                continue
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                # `from blitzecdn.cli import common` names a *module*, so the
                # thing to judge is `blitzecdn.cli.common` rather than the
                # package it was reached through — otherwise the allowlist
                # would have to admit all of `blitzecdn.cli` to admit one
                # helper module out of it.
                candidates = [f"{node.module}.{alias.name}" for alias in node.names]
                imported = (
                    node.module
                    if permitted(node.module)
                    or not all(permitted(name) for name in candidates)
                    else candidates[0]
                )
            elif isinstance(node, ast.Import):
                imported = node.names[0].name
            else:
                continue
            if not imported.startswith("blitzecdn.") and imported != "blitzecdn":
                continue
            if not permitted(imported):
                offenders.append(f"{path.name} imports {imported}")
    assert offenders == []


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_never_reaches_a_private_name_in_another(package: Path):
    """No `from blitzecdn.core.plugins._something import ...`, either way.

    A leading underscore is the only marker Python gives for "this is not the
    contract", and a cross-distribution import of one couples two release
    cadences to a name neither promised.
    """
    offenders = [
        f"{path.name} imports {imported}"
        for path in _source_files(package)
        for imported in sorted(_imports(path))
        if imported.startswith("blitzecdn")
        and any(part.startswith("_") for part in imported.split("."))
    ]
    assert offenders == []


# --- registration is metadata, and nothing else -----------------------------


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_registers_through_the_shared_entry_point_group(
    package: Path,
):
    """One group, reused. Not a second registry, and not a list in core.

    `blitzecdn.plugins` already existed for a package this repository has never
    heard of, so a capability that moved *out* of the distribution uses the
    same door an external one always did — which is what makes the built-in
    and the third-party cases the same case.
    """
    manifest = _manifest(package)
    groups = manifest["project"]["entry-points"]
    assert list(groups) == [ENTRY_POINT_GROUP]
    targets = groups[ENTRY_POINT_GROUP]
    assert targets, f"{package.name} declares the group and advertises nothing"
    for value in targets.values():
        assert value.startswith(f"{_import_package(package)}.")
        assert value.endswith(".plugin")


def test_entry_point_names_are_unique_across_the_installed_environment():
    """Two distributions cannot both answer to one name.

    Checked against the environment rather than the manifests: the collision
    that matters is between what is *installed*, which may include a package
    from outside this workspace entirely.
    """
    names = [point.name for point in entry_points(group=ENTRY_POINT_GROUP)]
    assert sorted(names) == sorted(set(names))


def test_the_installed_environment_advertises_every_workspace_package():
    """The manifests and the environment agree.

    A test that only read `pyproject.toml` would pass on a package that was
    never installed, and a package that is not installed contributes nothing —
    so this asserts the metadata `importlib` actually reports, which is the
    same read `register_external` performs.
    """
    advertised = {point.name for point in entry_points(group=ENTRY_POINT_GROUP)}
    expected = {
        name
        for package in _packages()
        for name in _manifest(package)["project"]["entry-points"][ENTRY_POINT_GROUP]
    }
    assert expected <= advertised


def test_an_optional_capability_is_discovered_only_through_its_entry_point():
    """The property the whole boundary exists for, stated as one assertion.

    Discovery with the group switched off is exactly the built-in set. Every
    capability an optional distribution supplies is absent from it and present
    with it, and nothing in core was consulted either way.
    """
    builtins = load_plugins(entry_point_group=None)
    installed = load_plugins()

    added = installed.capabilities - builtins.capabilities
    assert added, "no optional capability is installed in this environment"
    assert {"backup", "cache"} <= added
    assert not (added & builtins.capabilities)


# --- tests travel with their package ----------------------------------------


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_packages_tests_live_inside_it(package: Path):
    assert list((package / "tests").glob("test_*.py"))


def test_the_control_plane_suite_names_no_optional_package():
    """Core's tests do not import a capability that may not be installed.

    The exceptions are this file and its lifecycle companion, which are *about*
    the packages and would be meaningless without naming them. Everything else
    under `tests/` must pass with every optional distribution uninstalled,
    because that is the configuration `just test-core-only` runs.
    """
    optional = _optional_import_roots()
    here = {"test_packages.py", "test_lifecycle.py"}
    offenders = [
        f"{path.name} imports {imported}"
        for path in sorted(Path(__file__).parents[1].rglob("*.py"))
        if path.name not in here
        for imported in sorted(_imports(path))
        if imported.split(".")[0] in optional
    ]
    assert offenders == []


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
    assert "blitzecdn.features.http.plugin" in BUILTIN_PLUGINS


def test_the_http3_capability_is_reached_only_through_its_entry_point():
    """Attached and detached are both real, and neither touches core.

    The generic tests above already hold this for every package; stated once
    for `http3` too because the token is the one the site contract names, and a
    capability whose token nothing supplied would fail as a validation error on
    every HTTP/3 site rather than as anything obviously packaging-shaped.
    """
    builtins = load_plugins(entry_point_group=None)
    installed = load_plugins()

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
    plugin = SOURCE / "features/http/plugin.py"
    offenders = [
        "features/http/plugin.py reads .http3_enabled"
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
    what the v1/v2 schemas, the persisted policy JSON and the deployment
    snapshots all consume. Moving `http3_enabled` into the package would make
    that shape depend on what is installed, and a stored site asking for HTTP/3
    would stop loading on a controller that had detached it.
    """
    from blitzecdn.features.http.policy import ProtocolPolicy
    from blitzecdn.features.sites.domain import CdnSite

    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert "http3_enabled" in CdnSite.model_fields
    assert ProtocolPolicy.__module__.startswith("blitzecdn.features.http")


# --- GeoIP is optional; the settings that ask for a country are not ---------


#: The three stable fields that ask the edge which country a visitor is in.
#: Two belong to `SecurityPolicy` and one to the site's own header policy —
#: different owners, one capability behind them, which is why there is one
#: wheel rather than one per consumer.
_COUNTRY_SETTINGS = ("allowed_countries", "denied_countries", "ip_country")


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
    builtins = load_plugins(entry_point_group=None)
    installed = load_plugins()

    assert "geoip" not in builtins.capabilities
    assert "geoip" in installed.capabilities
    assert "geoip" not in {metadata.name for metadata in builtins.plugins}
    assert not any("geoip" in module for module in BUILTIN_PLUGINS)


def test_the_site_contract_keeps_every_country_setting_in_core():
    """The fields may not follow the implementation out.

    `CdnSite` composes them by inheritance into the flat shape the v1/v2
    schemas, the persisted policy JSON and the deployment snapshots consume.
    Moving one into the package would make that shape depend on what is
    installed, and a stored site asking for a country would stop loading on a
    controller that had detached it.
    """
    from blitzecdn.features.security.policy import SiteFirewall
    from blitzecdn.features.sites.domain import CdnSite
    from blitzecdn.features.sites.policy.headers import SiteVisitorHeaders

    assert {"allowed_countries", "denied_countries"} <= set(SiteFirewall.model_fields)
    assert "ip_country" in SiteVisitorHeaders.model_fields
    assert {"firewall", "visitor_headers"} <= set(CdnSite.model_fields)
    assert SiteFirewall.__module__.startswith("blitzecdn.features.security")
    assert SiteVisitorHeaders.__module__.startswith("blitzecdn.features.sites")


def test_the_country_settings_derive_their_token_generically_in_core():
    """The derivation is core's, and it is not written as a GeoIP special case.

    `capability_requirements` maps every stable setting onto the token it needs
    the same way, so `geoip` arrives by the same path as `compression` or
    `http3`. What this refuses is the shape the acceptance criteria warn about:
    a `registry.require("geoip")` sprinkled through unrelated services.
    """
    offenders = [
        f"{path.relative_to(SOURCE)} names the geoip token"
        for path in sorted(SOURCE.rglob("*.py"))
        if path != SOURCE / "features/sites/domain.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "geoip"
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


# --- the firewall belongs to the security capability, not to core -----------


#: The six kinds of rule a site firewall carries, and the vocabulary they are
#: validated against. `SiteFirewall` is the source of the first six, so a
#: seventh rule kind lands here without this list being edited.
def _firewall_rule_kinds() -> frozenset[str]:
    from blitzecdn.features.security.policy import SiteFirewall

    return frozenset(SiteFirewall.model_fields)


_FIREWALL_VOCABULARY = frozenset(
    {"COUNTRY_CODE", "COUNTRY_ALIASES", "ISO_3166_1_ALPHA_2", "HTTP_METHOD"}
)


def _names_and_string_constants(path: Path) -> set[str]:
    """Every identifier and string literal a module actually contains.

    Text search would answer differently: `core/validation.py` names
    `blitzecdn_firewall_ssh_port`, which is the *edge host's* SSH firewall — a
    different concept that reaches Ansible from the edge record, and one that
    a grep for "firewall" cannot tell apart from a site's request rules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg | ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
            found.update(node.value.split("."))
    return found


def test_core_knows_no_kind_of_firewall_rule():
    """`core/` names none of it — not a rule kind, not the vocabulary.

    The rule the whole extraction rests on, stated where it can fail. A firewall
    is the security capability's, and `blitzecdn-security` is a wheel an
    operator can leave out; core carrying `allowed_countries` or the ISO table
    would mean the control plane's generic infrastructure had memorised the
    settings of something detachable. Two leaks lived here and are gone: the
    country and HTTP-method tables in `core/validation.py`, whose only consumer
    was the security contract, and `if site.firewall.empty` in
    `core/ansible/mapping.py`, which named one capability's block inside a
    generic adapter.
    """
    forbidden = _firewall_rule_kinds() | _FIREWALL_VOCABULARY
    offenders = [
        f"{path.relative_to(SOURCE)} names {name}"
        for path in sorted((SOURCE / "core").rglob("*.py"))
        for name in sorted(_names_and_string_constants(path) & forbidden)
    ]
    assert offenders == []


#: Where a firewall rule kind may still be named outside its own contract, and
#: why. This list *is* the "remaining core knowledge" section of the
#: extraction: it is short, every entry is a deliberate contract, and a new
#: entry is a decision rather than an oversight.
#:
#: * the two versioned API models are frozen per-version *resource* shapes;
#:   v1 may not change when v2 does, so they restate the fields rather than
#:   re-export the contract;
#: * `dns/cli.py` carries `blitzecdn record firewall`, because a record patch
#:   is the DNS capability's surface and `dns -> security` is a declared
#:   contract edge in `ALLOWED_POLICY_DEPENDENCIES`;
#: * `sites/domain.py` names only the two *country* settings, to derive the
#:   `geoip` token — asserted separately by the GeoIP tests above.
_FIREWALL_AWARE_MODULES = {
    "features/security/policy.py": None,
    "api/v1_models.py": None,
    "api/v2_models.py": None,
    "features/dns/cli.py": None,
    "features/sites/domain.py": frozenset({"allowed_countries", "denied_countries"}),
}


def test_only_declared_modules_outside_the_contract_name_a_firewall_rule():
    forbidden = _firewall_rule_kinds() | _FIREWALL_VOCABULARY
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative not in _FIREWALL_AWARE_MODULES:
            allowed: frozenset[str] = frozenset()
        elif (permitted := _FIREWALL_AWARE_MODULES[relative]) is None:
            continue
        else:
            allowed = permitted
        offenders.extend(
            f"{relative} names {name}"
            for name in sorted(_names_and_string_constants(path) & forbidden - allowed)
        )
    assert offenders == []


def test_the_edge_document_prunes_blocks_by_declaration_not_by_name():
    """`site_to_ansible` asks whether a block opted in, never what it is.

    Both halves matter. The source may not name a capability's block, and the
    behaviour must still be the one the edge role and the committed contract
    fixture were written against: an empty firewall is *absent* from the
    document rather than present and empty, a configured one is there, and
    `visitor_headers` — which is not `OmittedWhenEmpty` — is never pruned,
    because a block that has never been configured is not the same as one whose
    switches are off.
    """
    from blitzecdn.core.ansible.mapping import site_to_ansible
    from blitzecdn.core.validation import OmittedWhenEmpty
    from blitzecdn.features.sites.domain import CdnSite

    source = (SOURCE / "core/ansible/mapping.py").read_text(encoding="utf-8")
    mapper = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "site_to_ansible"
    )
    # The docstring is exempt: it explains what the branch used to be, which is
    # the record of why the pruning is written this way.
    body = mapper.body[1:] if ast.get_docstring(mapper) else mapper.body
    assert "firewall" not in "".join(ast.unparse(node) for node in body)

    site = CdnSite(
        name="edge-doc",
        server_names=("edge-doc.example.com",),
        origin_host="198.51.100.10",
    )
    assert isinstance(site.firewall, OmittedWhenEmpty)
    assert not isinstance(site.visitor_headers, OmittedWhenEmpty)

    assert "firewall" not in site_to_ansible(site)
    assert "visitor_headers" in site_to_ansible(site)

    configured = site.model_copy(
        update={"firewall": type(site.firewall)(denied_methods=("DELETE",))}
    )
    assert site_to_ansible(configured)["firewall"] == {
        "allow_sources": [],
        "deny_sources": [],
        "allowed_countries": [],
        "denied_countries": [],
        "denied_methods": ["DELETE"],
        "denied_paths": [],
    }
