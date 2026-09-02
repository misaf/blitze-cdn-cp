"""Executable boundaries for the capability-oriented modular monolith.

Two graphs, not one, and the split is the architecture.

A capability's ``policy`` module is its **contract**: pure values describing how
the capability is configured, importing nothing but ``core`` and another
capability's contract. Its other modules are its **implementation**: the
services, adapters, routers and commands that act on those values.

``sites`` composes every capability's contract into one flat virtual host, and
most capabilities' implementations consume ``CdnSite``. Counting both as one
kind of edge would make that a cycle and force the contracts back into
``sites`` — which is exactly the "one feature owns every setting" shape this
replaced. So contract edges and implementation edges are declared and checked
separately, and the layer rule that keeps the whole thing a DAG is asserted
directly: **a contract never imports an implementation.**

    core  <  capability contracts  <  capability implementations
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
from paths import SOURCE

from blitzecdn.core.plugins import BUILTIN_PLUGINS

_SOURCE = SOURCE
_COMPOSITION_ROOT = "blitzecdn.bootstrap"
_FEATURES = _SOURCE / "features"
_IO_IMPORTS = (
    "fastapi",
    "starlette",
    "typer",
    "click",
    "sqlite3",
    "subprocess",
    "ansible",
    "dns",
    "certbot",
    "cryptography",
    "yaml",
    "redis",
    "dramatiq",
    "sqlalchemy",
    "sqlmodel",
)
_DOMAIN_FILES = {"domain.py", "origins.py", "snapshots.py"}
_ADAPTER_PARTS = {
    "adapters",
    # A capability small enough for one adapter module rather than a package
    # is still an adapter. `tls/certificates/adapters.py` was outside this set
    # purely because it is a file and the others are a directory.
    "adapters.py",
    "persistence.py",
    "site_persistence.py",
    "probe.py",
    "preflight.py",
    "desired_state.py",
}
_PUBLIC_CROSS_FEATURE_MODULES = {
    "domain",
    "origins",
    "policy",
    "ports",
    "reporting",
    "snapshots",
}

#: Capabilities large enough to be organised into named parts. A sub-capability
#: is not a feature: it has no `plugin.py`, it is not in `BUILTIN_PLUGINS`, and
#: it shares its parent's node in both dependency graphs. TLS is the only one,
#: and it exists because issuing material and deciding when to upgrade a mode
#: are genuinely different jobs on the same capability.
_SUB_CAPABILITIES: dict[str, set[str]] = {}

#: Names that must never become a top-level feature package. Each is a
#: strategy, a protocol version, a mode or an implementation detail of a
#: capability that already exists — `http3` and `certificates` were both
#: top-level once. A new one belongs *inside* its capability, and a developer
#: adding an option should be made to answer "which capability owns this?"
#: before a package appears. Mapped to the capability that owns it so the
#: failure says where the code should have gone.
_STRATEGIES_OWNED_BY_A_CAPABILITY = {
    "acme": "tls",
    "automatic_ssl": "tls",
    "brotli": "compression",
    "certbot": "tls",
    "certificates": "tls",
    "firewall": "security",
    "geoip": "security",
    "gzip": "compression",
    "http1": "http",
    "http2": "http",
    "http3": "http",
    "letsencrypt": "tls",
    "origin": "sites",
    "protocols": "http",
    "quic": "http",
    "ssl": "tls",
    "under_attack": "security",
    "visitor_headers": "sites",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _runtime_imports(path: Path) -> set[str]:
    """Imports that actually execute, ignoring `if TYPE_CHECKING:` blocks.

    An annotation-only import cannot create a cycle and cannot pull a package
    into a process, so where the rule is about *runtime* direction — what the
    plugin machinery loads, what a module makes the interpreter import — this
    is the honest question to ask. Where the rule is about knowledge rather than
    loading, `_imports` still counts every one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    typing_only = {
        node
        for branch in ast.walk(tree)
        if isinstance(branch, ast.If) and ast.unparse(branch.test) == "TYPE_CHECKING"
        for node in ast.walk(branch)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if node in typing_only:
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _module_scope_imports(path: Path) -> set[str]:
    """Only the imports the interpreter runs while importing this module.

    An import inside a function body executes when that function is *called*,
    so it cannot produce a circular import at load time — it is the documented
    way to break one, and `worker.py` and `core.schema` both use it
    deliberately. Counting it as an edge would make the cycle test refuse the
    remedy for the problem it exists to detect.

    Narrower than `_runtime_imports`, which only drops `TYPE_CHECKING`: this
    also drops anything nested in a `def`, `async def` or `class` body.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deferred = {
        node
        for scope in ast.walk(tree)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        or (isinstance(scope, ast.If) and ast.unparse(scope.test) == "TYPE_CHECKING")
        for node in ast.walk(scope)
        if node is not scope
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if node in deferred:
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _banned(imported: str, forbidden: tuple[str, ...]) -> bool:
    root = imported.split(".")[0]
    return any(
        root == name or imported == name or imported.startswith(f"{name}.")
        for name in forbidden
    )


def _feature_files() -> list[Path]:
    return sorted(_FEATURES.rglob("*.py"))


def _feature_name(path: Path) -> str:
    return path.relative_to(_FEATURES).parts[0]


def _policy_files() -> list[Path]:
    """Every capability contract module, wherever the capability keeps it.

    Both shapes count: `tls/policy.py`, one file because the contract is one
    cohesive set of TLS values, and `sites/policy/`, a package because a site's
    own configuration splits cleanly into cache, headers and origin.
    """
    return sorted(
        path
        for path in _feature_files()
        if "policy" in {part.removesuffix(".py") for part in path.parts}
    )


def _is_policy_import(imported: str) -> bool:
    """Whether an import names another capability's contract rather than it."""
    parts = imported.removeprefix("blitzecdn.features.").split(".")
    return len(parts) > 1 and parts[1] == "policy"


def test_legacy_layer_first_packages_have_no_source_modules():
    for package in ("domain", "application", "infrastructure"):
        assert not list((_SOURCE / package).rglob("*.py"))
    assert not list((_SOURCE / "api/routes").rglob("*.py"))


@pytest.mark.parametrize(
    "feature",
    [
        "compression",
        "deployments",
        "diagnostics",
        "dns",
        "edges",
        "http",
        "maintenance",
        "security",
        "sites",
        "tls",
    ],
)
def test_required_feature_packages_exist(feature: str):
    assert (_FEATURES / feature / "__init__.py").is_file()


def test_feature_domains_are_framework_and_io_independent():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _feature_files()
        if path.name in _DOMAIN_FILES or "policy" in path.relative_to(_FEATURES).parts
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                *_IO_IMPORTS,
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.bootstrap",
                "blitzecdn.core.ansible",
                "blitzecdn.core.broker",
                "blitzecdn.core.database",
                "blitzecdn.core.database_engine",
                "blitzecdn.core.database_models",
                "blitzecdn.core.filesystem",
                "blitzecdn.core.process",
            ),
        )
    ]
    assert offenders == []


#: Application policy that lives beside a service rather than inside it. Held
#: to the service rule, because moving code out of `service.py` must not be a
#: way to escape it.
_APPLICATION_MODULES = {"service.py", "rollback.py", "reporting.py"}


def test_feature_services_depend_on_contracts_not_concrete_adapters():
    forbidden = (
        *_IO_IMPORTS,
        "blitzecdn.api",
        "blitzecdn.cli",
        "blitzecdn.bootstrap",
        "blitzecdn.core.ansible",
        "blitzecdn.core.broker",
        "blitzecdn.core.database",
        "blitzecdn.core.database_engine",
        "blitzecdn.core.database_models",
    )
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _feature_files()
        if path.name in _APPLICATION_MODULES
        for imported in sorted(_imports(path))
        if _banned(imported, forbidden)
        or any(part in imported for part in (".adapters", ".persistence", ".probe"))
    ]
    assert offenders == []


def test_feature_adapters_never_import_entry_layers_or_composition():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _feature_files()
        if any(part in _ADAPTER_PARTS for part in path.parts)
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.bootstrap",
                "blitzecdn.worker",
            ),
        )
    ]
    assert offenders == []


def _entry_files() -> list[Path]:
    candidates = [
        *_feature_files(),
        *(_SOURCE / "api").rglob("*.py"),
        *(_SOURCE / "cli").rglob("*.py"),
    ]
    names = {
        "cli.py",
        "v1.py",
        "v2.py",
        "v1_sites.py",
        "v2_sites.py",
        "readiness.py",
    }
    return [
        path
        for path in candidates
        if path.name in names
        or _SOURCE / "api" in path.parents
        or _SOURCE / "cli" in path.parents
    ]


def test_entry_adapters_never_reach_persistence_or_database_directly():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _entry_files()
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                "blitzecdn.core.database",
                "blitzecdn.core.database_engine",
                "blitzecdn.core.database_models",
            ),
        )
        or ".persistence" in imported
    ]
    assert offenders == []


def test_cross_feature_imports_use_contract_modules():
    offenders: list[str] = []
    prefix = "blitzecdn.features."
    for path in _feature_files():
        owner = _feature_name(path)
        for imported in sorted(_imports(path)):
            if not imported.startswith(prefix):
                continue
            parts = imported.removeprefix(prefix).split(".")
            if parts[0] == owner or len(parts) == 1:
                continue
            if parts[1] in _SUB_CAPABILITIES.get(parts[0], set()):
                # A sub-capability's package is its public face, and below it
                # the same rule applies one level down.
                parts = parts[1:]
                if len(parts) == 1:
                    continue
            if parts[1] not in _PUBLIC_CROSS_FEATURE_MODULES:
                offenders.append(f"{path.relative_to(_SOURCE)} imports {imported}")
    assert offenders == []


def test_control_plane_is_the_only_production_composition_root():
    imports = _imports(_SOURCE / "bootstrap.py")
    assert any(name.startswith("blitzecdn.features") for name in imports)
    assert any(name.startswith("blitzecdn.core") for name in imports)
    assert not any(name.startswith("blitzecdn.worker") for name in imports)


#: The classes that reach the outside world: a subprocess, a socket, a
#: database file, a private key on disk. Choosing one is composition, not
#: application logic.
_CONCRETE_ADAPTERS = {
    "AnsibleRunner",
    "CertbotIssuer",
    "CertificatePreflight",
    "DramatiqBackgroundRunner",
    "Repository",
}

#: `bootstrap` is the composition root for the control plane. `acme_hook`
#: is a second, deliberate one: certbot runs it as a one-shot subprocess with
#: no control plane in the picture, and it builds the two adapters an HTTP-01
#: challenge needs and nothing else. Named here so a third does not appear
#: quietly beside them.
_COMPOSITION_MODULES = {"bootstrap.py", "acme_hook.py"}


def test_only_a_composition_root_names_a_concrete_adapter():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {name}"
        for path in _SOURCE.rglob("*.py")
        if path.name not in _COMPOSITION_MODULES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("blitzecdn.")
        and node.module != f"blitzecdn.{path.parent.name}.{path.stem}"
        for name in (alias.name for alias in node.names)
        if name in _CONCRETE_ADAPTERS
        # A package re-exporting its own adapter is that package's public face,
        # not a second place that chose it.
        and not node.module.startswith(
            "blitzecdn." + ".".join(path.relative_to(_SOURCE).parts[:-1])
        )
    ]
    assert offenders == []


def test_core_carries_no_cross_feature_application_service():
    """`core` is what a feature builds on, not a place to put a workflow.

    `MaintenanceService` lived here and orchestrated three features, which
    pointed the arrow back from the foundation into the tree it supports and
    hid a genuine vertical slice where nobody would look for it. It is
    `features/maintenance` now. Persistence is the deliberate exception:
    `core.database` bundles the feature stores because there is one SQLite
    file, and it imports their `persistence` modules to do it.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in (_SOURCE / "core").rglob("*.py")
        for imported in sorted(_imports(path))
        if imported.startswith("blitzecdn.features.")
        and imported.endswith((".service", ".adapters"))
    ]
    assert offenders == []


def test_worker_remains_an_entry_point_and_queue_direction_is_one_way():
    assert "blitzecdn.bootstrap" in _imports(_SOURCE / "worker.py")
    offenders = [
        str(path.relative_to(_SOURCE))
        for path in _SOURCE.rglob("*.py")
        if path.name != "worker.py"
        and any(name.startswith("blitzecdn.worker") for name in _imports(path))
    ]
    assert offenders == []
    broker_imports = _imports(_SOURCE / "core/broker.py")
    assert not any(
        name.startswith(("blitzecdn.worker", "blitzecdn.bootstrap"))
        for name in broker_imports
    )


def test_entry_layers_cannot_reach_private_control_plane_adapters_or_stores():
    forbidden = {
        "_runner",
        "_issuer",
        "_preflight",
        "_background",
        "_certificate_store",
        "_origin_probe",
        "_edges_store",
        "repository",
        "database",
        "audit_log",
    }
    offenders = [
        f"{path.relative_to(_SOURCE)}:{node.lineno} reads .{node.attr}"
        for path in _entry_files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert offenders == []


def test_no_internal_module_dependency_cycles():
    """No module in this distribution can fail to import because of another.

    Module-scope imports only. A deferred import is how a genuine two-way
    dependency is made safe — `core.schema` owns the migration tree that
    `core.database_engine` migrates with, and reaches back for the engine
    inside the one method that runs one — and the loader never sees a loop.
    """
    modules = {
        "blitzecdn." + ".".join(path.relative_to(_SOURCE).with_suffix("").parts): path
        for path in _SOURCE.rglob("*.py")
        if "migrations" not in path.parts
    }
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        graph[module].update(
            name for name in _module_scope_imports(path) if name in modules
        )
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            cycle = [*visiting[visiting.index(module) :], module]
            pytest.fail("internal import cycle: " + " -> ".join(cycle))
        if module in visited:
            return
        visiting.append(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        visiting.pop()
        visited.add(module)

    for module in sorted(modules):
        visit(module)


def test_domain_models_do_not_render_adapter_documents():
    forbidden = {"to_ansible", "to_inventory", "to_api", "to_http"}
    offenders = [
        f"{path.relative_to(_SOURCE)}:{node.lineno} defines {node.name}"
        for path in _feature_files()
        if path.name in _DOMAIN_FILES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in forbidden
    ]
    assert offenders == []


def test_removed_subsystems_do_not_return():
    offenders = [
        str(path.relative_to(_SOURCE))
        for path in _SOURCE.rglob("*.py")
        if "outbox" in path.read_text(encoding="utf-8").lower()
        or "ThreadBackgroundRunner" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


#: Which feature may know that another exists. Derived from the graph the code
#: actually has, and enforced both ways: an undeclared edge fails, and so does
#: a declared edge nothing uses any more.
#:
#: Implementation edges only — see `ALLOWED_POLICY_DEPENDENCIES` for the
#: contract layer. The direction is the point. `sites` composes the capability
#: contracts and knows no other implementation. DNS derives sites from records;
#: cache, TLS, deployments and edges consume public site contracts without
#: reaching into DNS persistence or adapters. Diagnostics reports on the
#: capabilities above it.
#:
#: Shared foundations under `core` are deliberately outside this graph: `core`
#: is what a feature is allowed to build on without that counting as knowing
#: another feature.
ALLOWED_FEATURE_DEPENDENCIES = {
    "compression": set(),
    "deployments": {"dns", "sites"},
    "diagnostics": set(),
    "dns": {"sites"},
    # `dns` left this set with `check_origins`. Probing an origin needed the
    # site list, and `edges` reached it through `dns.ports.SiteStore` — the one
    # place the fleet roster depended on the zone feature. The roster itself
    # never needed a site: `blitzecdn-origins` reads `platform.sites` for
    # itself, and an edge is added, updated and removed without either.
    "edges": {"sites"},
    "http": {"sites"},
    "maintenance": {"deployments"},
    "security": set(),
    "sites": set(),
    "tls": set(),
}

#: Which capability's *contract* another may compose. Separate from the graph
#: above and pointing the other way in three places, which is the whole reason
#: it is separate: `sites` composes the compression, HTTP, security and TLS
#: contracts, while those capabilities' implementations consume the `CdnSite`
#: that composition produces.
#:
#: `http` and `security` appear in both. Their contracts depend on nothing;
#: their `plugin.py` annotates a hook with `CdnSite`, which is knowledge of
#: `sites` even under `TYPE_CHECKING` and is declared rather than excused.
ALLOWED_POLICY_DEPENDENCIES = {
    "compression": set(),
    "deployments": set(),
    "diagnostics": set(),
    "dns": {"compression", "security", "sites", "tls"},
    "edges": {"http", "tls"},
    "http": set(),
    "maintenance": set(),
    "security": set(),
    "sites": {"compression", "http", "security", "tls"},
    "tls": {"http"},
}


def test_a_capability_contract_never_imports_an_implementation():
    """The layer rule, and the only reason two graphs are safe to have.

    A `policy` module may name `core` and another capability's `policy`. The
    moment one reaches for a service, an adapter, a `domain` or a `ports`
    module, the contract layer stops being below the implementation layer and
    the two declared graphs stop composing into an order at all.
    """
    prefix = "blitzecdn.features."
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _policy_files()
        for imported in sorted(_imports(path))
        if imported.startswith(prefix) and not _is_policy_import(imported)
    ]
    assert offenders == []


def test_sites_composes_the_capability_contracts_and_owns_no_other_capability():
    """`sites` is the composition, not the owner of every setting.

    It was: compression, HTTP, security and TLS policy all lived under
    `sites/policy/` while the behaviour they describe lived in features that
    imported `sites` to reach it. Reuniting each contract with its capability
    is what this asserts, from both directions — `sites` imports the four, and
    defines none of them.
    """
    graph = _feature_graph()
    assert graph["sites"] == set()
    assert _policy_graph()["sites"] == {"compression", "http", "security", "tls"}
    assert "sites" in graph["dns"]

    site_imports = _imports(_FEATURES / "sites/domain.py")
    assert all("dns" not in imported for imported in site_imports)
    for capability in ("compression", "http", "security", "tls"):
        assert f"blitzecdn.features.{capability}.policy" in site_imports

    owned = {path.stem for path in (_FEATURES / "sites/policy").glob("*.py")}
    assert owned == {"__init__", "cache", "headers", "origin"}


def test_no_strategy_mode_or_option_becomes_a_top_level_feature():
    """The fundamental rule, checked by name.

    A top-level package under `features/` is a product capability. gzip and
    Brotli are strategies of compression; HTTP/1, /2 and /3 are versions of one
    protocol; Under Attack Mode is a mode of security; certificate issuance and
    the Automatic SSL/TLS scan are parts of TLS. Each of these has been, or
    could plausibly become, a package of its own — `http3` and `certificates`
    both were — and each time the result is a feature list that answers "what
    settings exist" instead of "what can this product do".
    """
    packages = {
        path.name
        for path in _FEATURES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    offenders = [
        f"features/{name} belongs inside the {owner} capability"
        for name in sorted(packages)
        for owner in [_STRATEGIES_OWNED_BY_A_CAPABILITY.get(name)]
        if owner is not None
    ]
    assert offenders == []


def test_a_sub_capability_is_not_a_feature():
    """`tls/certificates` is organisation inside a capability, not a capability.

    It has no `plugin.py`, it is not in `BUILTIN_PLUGINS`, and it shares TLS's
    node in both graphs. Giving it one of its own is how `certificates` and
    `automatic_ssl` came to be two owners of `SslMode`.
    """
    for parent, children in _SUB_CAPABILITIES.items():
        for child in children:
            package = _FEATURES / parent / child
            assert (package / "__init__.py").is_file()
            assert not (package / "plugin.py").exists()
            assert f"blitzecdn.features.{parent}.{child}.plugin" not in BUILTIN_PLUGINS


def test_policy_dependencies_match_what_is_declared():
    assert _policy_graph() == ALLOWED_POLICY_DEPENDENCIES


def test_the_capability_contract_graph_is_acyclic():
    _assert_acyclic(_policy_graph(), "capability contract")


#: Which `ControlPlane` attribute belongs to which feature. A `plugin.py` that
#: reads one is depending on that feature just as surely as an import would, so
#: `_feature_graph` counts both — otherwise moving a call from a service into a
#: registration hook would be a way to leave the declared graph.
_PLATFORM_SERVICES = {
    # Two attributes, one owner. `certificates` and `automatic_ssl` are parts
    # of the TLS capability, so a plugin reading either is depending on `tls`.
    "automatic_ssl": "tls",
    "certificates": "tls",
    "deployments": "deployments",
    "dns": "dns",
    "edges": "edges",
    "maintenance": "maintenance",
}

#: What every plugin may read off the platform without that being a dependency
#: on a feature: configuration, the cross-cutting journals, and the registry's
#: own accessors.
_PLATFORM_COMMON = {
    "audit",
    "broker_ready",
    "close",
    "events",
    # The two contracts an installed distribution builds itself from. Both are
    # ports rather than services, and neither belongs to a capability: `fleet`
    # runs a named play and knows nothing about what any play is for, and
    # `sites` is the read side of the model every capability already consumes.
    # A plugin reading either is not depending on a feature, which is the whole
    # reason they are shaped this way.
    "fleet",
    "health_checks",
    "jobs",
    "plugins",
    "process",
    "settings",
    "sites",
    "start",
    "stop",
    "workflow_history",
    "workflows",
}


def _platform_reads(path: Path) -> set[str]:
    """Every `platform.<name>` this module reads."""
    return {
        node.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "platform"
    }


def _graphs() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The implementation graph and the contract graph, in that order.

    An import of `blitzecdn.features.<other>.policy` is a *contract* edge and
    lands in the second. Everything else — the package itself, a domain module,
    a port, a service — is an implementation edge and lands in the first.
    """
    prefix = "blitzecdn.features."
    implementation: dict[str, set[str]] = {
        name: set() for name in ALLOWED_FEATURE_DEPENDENCIES
    }
    contract: dict[str, set[str]] = {
        name: set() for name in ALLOWED_POLICY_DEPENDENCIES
    }
    for path in _feature_files():
        owner = _feature_name(path)
        if owner not in implementation:
            continue
        for imported in _imports(path):
            if not imported.startswith(prefix):
                continue
            depends_on = imported.removeprefix(prefix).split(".")[0]
            # Any form counts, `from blitzecdn.features.x import y` included:
            # importing another feature's package is depending on it.
            if depends_on == owner:
                continue
            target = contract if _is_policy_import(imported) else implementation
            target[owner].add(depends_on)
    for path in _FEATURES.glob("*/plugin.py"):
        owner = _feature_name(path)
        for attribute in _platform_reads(path):
            depends_on = _PLATFORM_SERVICES.get(attribute)
            if depends_on is not None and depends_on != owner:
                implementation[owner].add(depends_on)
    return implementation, contract


def _feature_graph() -> dict[str, set[str]]:
    return _graphs()[0]


def _policy_graph() -> dict[str, set[str]]:
    return _graphs()[1]


def _assert_acyclic(graph: dict[str, set[str]], label: str) -> None:
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            cycle = [*visiting[visiting.index(node) :], node]
            pytest.fail(f"{label} cycle: " + " -> ".join(cycle))
        if node in done:
            return
        visiting.append(node)
        for dependency in sorted(graph[node]):
            visit(dependency)
        visiting.pop()
        done.add(node)

    for node in sorted(graph):
        visit(node)


def test_every_feature_package_is_in_the_declared_graph():
    packages = {
        path.name
        for path in _FEATURES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert packages == set(ALLOWED_FEATURE_DEPENDENCIES)


def test_feature_dependencies_match_what_is_declared():
    assert _feature_graph() == ALLOWED_FEATURE_DEPENDENCIES


def test_the_feature_dependency_graph_is_acyclic():
    """A cycle between features is a modular monolith turning back into a ball.

    There were four, and every one of them ran through a single fat port:
    `DeploymentRunner` used to declare the purge, stats, origin-check and
    decommission plays as well as the deploy, so `cache` and `edges` had to
    import the deployment package to reach their own playbook, while
    `deployments` imported `cache.domain` for the purge entries it named and
    `certificates.ports` for two certificate paths. Each feature declares the
    slice it uses now, the composition root is the only place that knows one
    adapter satisfies all of them, and the graph is a DAG.
    """
    _assert_acyclic(_feature_graph(), "feature dependency")


def test_no_feature_port_declares_another_feature_s_playbook():
    """The rule that keeps the graph a DAG rather than merely making it one.

    A port belongs to whoever calls it. The moment one feature's port module
    describes a run another feature performs, that other feature has to import
    this one to type its own collaborator — which is exactly how the cycles got
    there.
    """
    # `run_cache_purge`, `run_stats` and `run_origin_check` belong to no
    # feature at all any more — they left with the distributions that run
    # those plays, and a core `ports.py` naming one would be core declaring an
    # optional capability's operation. `None` says exactly that: no feature
    # may declare it, not even the one it used to live beside.
    owners: dict[str, str | None] = {
        "run_cache_purge": None,
        "run_stats": None,
        "run_origin_check": None,
        "run_decommission": "edges",
    }
    offenders = [
        f"{path.relative_to(_SOURCE)} declares {node.name}, which belongs to {owner}"
        for path in _FEATURES.glob("*/ports.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        for owner in [owners.get(node.name, _feature_name(path))]
        if owner != _feature_name(path)
    ]
    assert offenders == []


def test_every_feature_registers_itself_through_a_plugin_module():
    """A feature the plugin manager has never heard of is a feature nothing runs.

    Both directions: a package without a `plugin.py` contributes nothing, and a
    `plugin.py` missing from `BUILTIN_PLUGINS` is never imported — either way
    the routes and commands quietly are not there, which is exactly the failure
    a discovery mechanism is supposed to make impossible.
    """
    packages = {
        path.name
        for path in _FEATURES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    contract_only = {"compression", "security"}
    assert {path.parent.name for path in _FEATURES.glob("*/plugin.py")} == (
        packages - contract_only
    )
    assert set(BUILTIN_PLUGINS) == {
        f"blitzecdn.features.{name}.plugin" for name in packages - contract_only
    }


def test_only_a_plugin_module_may_name_the_composition_root():
    """Registration may know the platform. Everything else in a feature may not.

    `plugin.py` is handed the built control plane so it can say which service a
    scheduled job calls and which one a health check probes. That is the whole
    of the allowance: a service, a domain module or an adapter that reached for
    it would be resolving its collaborators at call time instead of receiving
    them, which is the service-locator architecture this design refuses.

    A feature's *entry* modules are outside the rule for the same reason
    `cli/common.py` is: a command has to get a control plane from somewhere.
    `backup/cli.py` is the pointed case — it builds a backup service directly,
    because restore has to work on a host where the control plane cannot start.
    """
    entry = set(_entry_files())
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _feature_files()
        if path.name != "plugin.py" and path not in entry
        for imported in sorted(_imports(path))
        if imported.startswith(_COMPOSITION_ROOT)
    ]
    assert offenders == []


def test_a_plugin_module_names_the_composition_root_only_for_typing():
    """And even there, never at runtime.

    A feature importing the composition root for real would point the arrow
    back at the thing that builds it. `plugin.py` needs the *type* to annotate
    its hooks, which `TYPE_CHECKING` gives it without an import ever executing.
    """
    offenders: list[str] = []
    for path in _FEATURES.glob("*/plugin.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = {
            node
            for branch in ast.walk(tree)
            if isinstance(branch, ast.If)
            and ast.unparse(branch.test) == "TYPE_CHECKING"
            for node in ast.walk(branch)
        }
        offenders.extend(
            f"{path.relative_to(_SOURCE)}:{node.lineno} imports the composition root"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith(_COMPOSITION_ROOT)
            and node not in guarded
        )
    assert offenders == []


def test_a_plugin_reads_only_the_platform_services_its_graph_declares():
    """`platform.x` in a registration hook is a declared dependency or a bug."""
    offenders = [
        f"{path.relative_to(_SOURCE)} reads platform.{attribute}"
        for path in _FEATURES.glob("*/plugin.py")
        for attribute in sorted(_platform_reads(path))
        if attribute not in _PLATFORM_COMMON
        and _PLATFORM_SERVICES.get(attribute)
        not in {
            _feature_name(path),
            *ALLOWED_FEATURE_DEPENDENCIES[_feature_name(path)],
        }
    ]
    assert offenders == []


def test_the_entry_layers_import_no_feature_at_all():
    """The point of the whole exercise, asserted where it can be checked.

    `api/app.py` used to name seventeen router modules and `cli/main.py` eleven
    command groups, so adding a feature meant editing both. They ask the plugin
    registry now, and a feature that appears in neither list is a feature that
    forgot to contribute — not one somebody forgot to wire.

    Only these two modules. `api/v1_models.py` projects domain values into the
    frozen HTTP representations and has to name them; that is a *translation*,
    and it is the assembly that had to stop knowing the feature list.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in (_SOURCE / "api/app.py", _SOURCE / "cli/main.py")
        for imported in sorted(_imports(path))
        if imported.startswith("blitzecdn.features")
    ]
    assert offenders == []


def test_the_plugin_infrastructure_depends_on_no_feature():
    """`core.plugins` is the mechanism, not a participant in it.

    Runtime imports only. The hookspecs annotate their arguments with the
    control plane and with `CdnSite`, because a specification with untyped
    parameters would be no specification at all — but nothing is imported to
    do it, so the direction the interpreter actually takes stays one way.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in (_SOURCE / "core/plugins").rglob("*.py")
        for imported in sorted(_runtime_imports(path))
        if imported.startswith(("blitzecdn.features", _COMPOSITION_ROOT))
    ]
    assert offenders == []
