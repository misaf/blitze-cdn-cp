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
        for path in _FEATURES.glob("*/service.py")
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
