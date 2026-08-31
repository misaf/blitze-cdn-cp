"""Executable boundaries for the package-by-feature modular monolith."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from blitzecdn.core.plugins import BUILTIN_PLUGINS

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "blitzecdn"
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


def test_legacy_layer_first_packages_have_no_source_modules():
    for package in ("domain", "application", "infrastructure"):
        assert not list((_SOURCE / package).rglob("*.py"))
    assert not list((_SOURCE / "api/routes").rglob("*.py"))


@pytest.mark.parametrize(
    "feature",
    [
        "automatic_ssl",
        "backup",
        "cache",
        "certificates",
        "deployments",
        "diagnostics",
        "dns",
        "edges",
        "maintenance",
        "sites",
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
        "tls_cli.py",
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
            if len(parts) > 1 and parts[1] not in _PUBLIC_CROSS_FEATURE_MODULES:
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
    modules = {
        "blitzecdn." + ".".join(path.relative_to(_SOURCE).with_suffix("").parts): path
        for path in _SOURCE.rglob("*.py")
        if "migrations" not in path.parts
    }
    graph: dict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        graph[module].update(name for name in _imports(path) if name in modules)
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
#: The direction is the point. `sites` owns the site-serving contract and knows
#: no other feature. DNS derives sites from records; cache, certificates,
#: deployments, and edges consume public site contracts without reaching into
#: DNS persistence or adapters. Diagnostics reports on capabilities above them.
#:
#: Shared foundations under `core` are deliberately outside this graph: `core`
#: is what a feature is allowed to build on without that counting as knowing
#: another feature.
ALLOWED_FEATURE_DEPENDENCIES = {
    "automatic_ssl": {"deployments", "dns", "edges", "sites"},
    "backup": set(),
    "cache": {"dns", "sites"},
    "certificates": {"deployments", "dns", "edges", "sites"},
    "deployments": {"dns", "sites"},
    "diagnostics": {"cache", "certificates"},
    "dns": {"sites"},
    "maintenance": {"automatic_ssl", "certificates", "deployments"},
    "edges": {"dns", "sites"},
    "sites": set(),
}


def test_sites_is_the_dependency_base_for_site_serving_policy():
    graph = _feature_graph()
    assert graph["sites"] == set()
    assert "sites" in graph["dns"]
    assert all(
        "dns" not in imported for imported in _imports(_FEATURES / "sites/domain.py")
    )


#: Which `ControlPlane` attribute belongs to which feature. A `plugin.py` that
#: reads one is depending on that feature just as surely as an import would, so
#: `_feature_graph` counts both — otherwise moving a call from a service into a
#: registration hook would be a way to leave the declared graph.
_PLATFORM_SERVICES = {
    "automatic_ssl": "automatic_ssl",
    "backup": "backup",
    "cache": "cache",
    "certificates": "certificates",
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
    "health_checks",
    "jobs",
    "plugins",
    "process",
    "settings",
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


def _feature_graph() -> dict[str, set[str]]:
    prefix = "blitzecdn.features."
    graph: dict[str, set[str]] = {name: set() for name in ALLOWED_FEATURE_DEPENDENCIES}
    for path in _feature_files():
        owner = _feature_name(path)
        if owner not in graph:
            continue
        for imported in _imports(path):
            if not imported.startswith(prefix):
                continue
            depends_on = imported.removeprefix(prefix).split(".")[0]
            # Any form counts, `from blitzecdn.features.x import y` included:
            # importing another feature's package is depending on it.
            if depends_on != owner:
                graph[owner].add(depends_on)
    for path in _FEATURES.glob("*/plugin.py"):
        owner = _feature_name(path)
        for attribute in _platform_reads(path):
            depends_on = _PLATFORM_SERVICES.get(attribute)
            if depends_on is not None and depends_on != owner:
                graph[owner].add(depends_on)
    return graph


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
    graph = _feature_graph()
    visiting: list[str] = []
    done: set[str] = set()

    def visit(feature: str) -> None:
        if feature in visiting:
            cycle = [*visiting[visiting.index(feature) :], feature]
            pytest.fail("feature dependency cycle: " + " -> ".join(cycle))
        if feature in done:
            return
        visiting.append(feature)
        for dependency in sorted(graph[feature]):
            visit(dependency)
        visiting.pop()
        done.add(feature)

    for feature in sorted(graph):
        visit(feature)


def test_no_feature_port_declares_another_feature_s_playbook():
    """The rule that keeps the graph a DAG rather than merely making it one.

    A port belongs to whoever calls it. The moment one feature's port module
    describes a run another feature performs, that other feature has to import
    this one to type its own collaborator — which is exactly how the cycles got
    there.
    """
    owners = {
        "run_cache_purge": "cache",
        "run_stats": "cache",
        "run_origin_check": "edges",
        "run_decommission": "edges",
    }
    offenders = [
        f"{path.relative_to(_SOURCE)} declares {node.name}, which belongs to {owner}"
        for path in _FEATURES.glob("*/ports.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        for owner in [owners.get(node.name)]
        if owner is not None and owner != _feature_name(path)
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
    assert {path.parent.name for path in _FEATURES.glob("*/plugin.py")} == packages
    assert set(BUILTIN_PLUGINS) == {
        f"blitzecdn.features.{name}.plugin" for name in packages
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
