"""Executable boundaries for the package-by-feature modular monolith."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "src" / "blitzecdn"
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
_DOMAIN_FILES = {"domain.py", "site_domain.py", "origins.py", "snapshots.py"}
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
    "site_domain",
    "origins",
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
    ],
)
def test_required_feature_packages_exist(feature: str):
    assert (_FEATURES / feature / "__init__.py").is_file()


def test_feature_domains_are_framework_and_io_independent():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _feature_files()
        if path.name in _DOMAIN_FILES
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                *_IO_IMPORTS,
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.control_plane",
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
        "blitzecdn.control_plane",
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
                "blitzecdn.control_plane",
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
    imports = _imports(_SOURCE / "control_plane.py")
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

#: `control_plane` is the composition root for the control plane. `acme_hook`
#: is a second, deliberate one: certbot runs it as a one-shot subprocess with
#: no control plane in the picture, and it builds the two adapters an HTTP-01
#: challenge needs and nothing else. Named here so a third does not appear
#: quietly beside them.
_COMPOSITION_MODULES = {"control_plane.py", "acme_hook.py"}


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
    assert "blitzecdn.control_plane" in _imports(_SOURCE / "worker.py")
    offenders = [
        str(path.relative_to(_SOURCE))
        for path in _SOURCE.rglob("*.py")
        if path.name != "worker.py"
        and any(name.startswith("blitzecdn.worker") for name in _imports(path))
    ]
    assert offenders == []
    broker_imports = _imports(_SOURCE / "core/broker.py")
    assert not any(
        name.startswith(("blitzecdn.worker", "blitzecdn.control_plane"))
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
#: The direction is the point. Everything flows towards `dns`, which owns the
#: zone records and the `CdnSite` projection every other feature reads and none
#: of them writes. `deployments` converges what `dns` declares; `certificates`
#: and `automatic_ssl` sit above both because issuing and upgrading are
#: decisions taken about a deployed site; `diagnostics` reports on the rest and
#: is depended on by nothing.
#:
#: Shared foundations under `core` are deliberately outside this graph: `core`
#: is what a feature is allowed to build on without that counting as knowing
#: another feature.
ALLOWED_FEATURE_DEPENDENCIES = {
    "automatic_ssl": {"deployments", "dns", "edges"},
    "backup": set(),
    "cache": {"dns"},
    "certificates": {"deployments", "dns", "edges"},
    "deployments": {"dns"},
    "diagnostics": {"cache", "certificates"},
    "dns": set(),
    "maintenance": {"automatic_ssl", "certificates", "deployments"},
    "edges": {"dns"},
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
