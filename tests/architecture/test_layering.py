"""Executable boundaries for the capability-oriented modular monolith.

Two graphs, not one. A capability's ``contract`` module holds pure values
describing its configuration and imports only ``core`` and other contracts.
Its other modules are its implementation.

``dns`` composes every contract into one virtual host, and most
implementations consume ``CdnSite``. Counted as one graph that would be a
cycle, so contract and implementation edges are declared and checked
separately, and the rule that keeps the whole a DAG is asserted directly:
**a contract never imports an implementation.**

    core  <  capability contracts  <  capability implementations

import-linter (`.importlinter`, run by `just lint`) checks which module may
import which. What is left here is what an import graph cannot express: names
and strings, files that must or must not exist, the declared capability graphs
(an unused declared edge fails too), allowlists of submodules another
capability may name, and module-scope import cycles.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
from support.paths import CORE_ANSIBLE, REPO_ROOT, SOURCE

from blitzecdn.composition import BUILTIN_FEATURES

_SOURCE = SOURCE
_COMPOSITION_ROOT = "blitzecdn.composition"
_CAPABILITIES = _SOURCE / "capabilities"


#: A slice's parts are named by the directory they sit in, not by a list kept
#: here, so a module is inside the rule the moment it is put where it belongs.
#: The `removesuffix(".py")` below admits `contract.py` and `cli.py`, the two
#: layers a capability may spell either way.
def _is_domain(path: Path) -> bool:
    """A capability's values: `domain/`, and its `contract`."""
    parts = path.relative_to(_CAPABILITIES).parts
    return "domain" in {part.removesuffix(".py") for part in parts} or "contract" in {
        part.removesuffix(".py") for part in parts
    }


_PUBLIC_CROSS_CAPABILITY_MODULES = {
    "domain",
    "contract",
    "ports",
}

#: Names that must never become a top-level capability package: each is a
#: strategy, protocol version, mode or detail of an existing capability. Mapped to
#: the owner so the failure says where the code belongs.
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


def _module_scope_imports(path: Path) -> set[str]:
    """Only the imports the interpreter runs while importing this module.

    A deferred import cannot cause a load-time cycle and is the usual way to break
    one, so `TYPE_CHECKING` blocks and imports nested in a `def` or `class` body
    are dropped. import-linter counts them like any other.
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


def _capability_files() -> list[Path]:
    return sorted(_CAPABILITIES.rglob("*.py"))


def _capability_name(path: Path) -> str:
    return path.relative_to(_CAPABILITIES).parts[0]


def _contract_files() -> list[Path]:
    """Every capability contract module: `contract.py` or a `contract/` package."""
    return sorted(
        path
        for path in _capability_files()
        if "contract" in {part.removesuffix(".py") for part in path.parts}
    )


def _is_contract_import(imported: str) -> bool:
    """Whether an import names another capability's contract rather than it."""
    parts = imported.removeprefix("blitzecdn.capabilities.").split(".")
    return len(parts) > 1 and parts[1] == "contract"


def test_legacy_layer_first_packages_have_no_source_modules():
    for package in ("domain", "application", "infrastructure"):
        assert not list((_SOURCE / package).rglob("*.py"))
    assert not list((_SOURCE / "api/routes").rglob("*.py"))


#: A capability's contact with a caller: everything under `api/` and `cli`.
#:
#: Positional, like `adapters/`, so a new delivery module is under the rule
#: without being listed. They stay outside `adapters/` because `api.models` is a
#: published contract while adapters must stay unreachable.
def _is_entry(path: Path) -> bool:
    parts = path.relative_to(_CAPABILITIES).parts
    return "api" in parts or "cli" in {part.removesuffix(".py") for part in parts}


def _entry_files() -> list[Path]:
    return [
        *(path for path in _capability_files() if _is_entry(path)),
        *(_SOURCE / "entrypoints/api").rglob("*.py"),
        *(_SOURCE / "entrypoints/cli").rglob("*.py"),
    ]


def test_cross_capability_imports_use_contract_modules():
    offenders: list[str] = []
    prefix = "blitzecdn.capabilities."
    for path in _capability_files():
        owner = _capability_name(path)
        for imported in sorted(_imports(path)):
            if not imported.startswith(prefix):
                continue
            parts = imported.removeprefix(prefix).split(".")
            if parts[0] == owner or len(parts) == 1:
                continue
            # HTTP representation reuse belongs to delivery. Concrete
            # runtime implementations are wired only in the composition root.
            # And a command editing a zone's policy changes it through dns.
            published = parts[1:] in (["api", "models"], ["cli", "editing"])
            if parts[1] not in _PUBLIC_CROSS_CAPABILITY_MODULES and not (
                published and _is_entry(path)
            ):
                offenders.append(f"{path.relative_to(_SOURCE)} imports {imported}")
    assert offenders == []


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


#: `blitzecdn.composition` is the control plane's composition root. Membership is
#: the directory, so a new module there is inside the allowance.
def _is_composition(path: Path) -> bool:
    return _SOURCE / "composition" in path.parents


def test_only_a_composition_root_names_a_concrete_adapter():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {name}"
        for path in _SOURCE.rglob("*.py")
        if not _is_composition(path)
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


def test_core_names_no_capability_even_in_a_string():
    """The same rule, asked of the text rather than of the import graph.

    A module path in a string is still knowledge of a capability, and the roster
    of capabilities belongs in `blitzecdn.composition`. Docstrings are exempt, so
    core may say where the roster lives.
    """
    offenders: list[str] = []
    for path in sorted((_SOURCE / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(scope.body[0].value)
            for scope in ast.walk(tree)
            if isinstance(
                scope,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and scope.body
            and isinstance(scope.body[0], ast.Expr)
            and isinstance(scope.body[0].value, ast.Constant)
        }
        offenders.extend(
            f"{path.relative_to(_SOURCE)}:{node.lineno} names {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "blitzecdn.capabilities" in node.value
            and id(node) not in docstrings
        )
    assert offenders == []


#: Where a process is actually started from, other than a Python import: the
#: installer, the compose files an edge and a controller converge to, and the
#: project's own scripts and recipes.
def _deployment_artifacts() -> list[Path]:
    return [
        REPO_ROOT / "install.sh",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "justfile",
        *sorted(CORE_ANSIBLE.rglob("*.j2")),
        # The role that copies the host runner out of the package.
        *sorted(CORE_ANSIBLE.rglob("tasks/*.yml")),
    ]


def test_a_loose_module_at_the_root_starts_a_process():
    """Loose modules in `entrypoints/` and `host/` start processes; the package root
    holds none.

    A process is checked rather than declared: something outside Python must name
    the module (the compose files, `install.sh`, the scripts and recipes). A helper
    dropped beside them is named by nothing and fails. `api/` and `cli/` are
    packages because each has a surface a single module would not hold.
    """
    artifacts = "\n".join(
        path.read_text(encoding="utf-8") for path in _deployment_artifacts()
    )
    offenders = [
        f"{path.name} is not started by anything outside Python"
        for path in sorted(
            [
                *SOURCE.glob("*.py"),
                *(SOURCE / "entrypoints").glob("*.py"),
                *(SOURCE / "host").glob("*.py"),
            ]
        )
        if path.name != "__init__.py"
        and path.parent != SOURCE
        and f"blitzecdn.{path.parent.name}.{path.stem}" not in artifacts
        and f"/{path.parent.name}/{path.name}" not in artifacts
    ] + [
        f"{path.name} is a loose module at the package root"
        for path in sorted(SOURCE.glob("*.py"))
        if path.name != "__init__.py"
    ]
    assert offenders == []


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

    Module-scope imports only. Deferred imports keep runtime-only integrations
    outside the import graph; core schema lifecycle owns no repository imports.
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
        for path in _capability_files()
        if _is_domain(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in forbidden
    ]
    assert offenders == []


#: Which capability may know that another exists, enforced both ways: an
#: undeclared edge fails, and so does a declared edge nothing uses.
#:
#: DNS owns zones, records, rules and their projection; shared serving values
#: belong to serving. A capability whose contract a zone carries depends on dns
#: for its `blitzecdn domain` commands.
ALLOWED_CAPABILITY_DEPENDENCIES = {
    "backup": set(),
    "origins": {"edges", "jobs", "serving"},
    "cache": {"dns", "jobs", "serving"},
    "compression": {"dns"},
    "deployments": {"dns", "edges", "releases", "serving", "workflows"},
    # Readiness and metrics read the state these own.
    "diagnostics": {"deployments", "dns", "edges", "jobs", "tls", "workflows"},
    "dns": {"serving"},
    "edges": {"jobs", "serving"},
    "hardening": set(),
    "http": {"dns", "serving"},
    "jobs": set(),
    "maintenance": {"deployments"},
    "releases": {"dns", "edges", "serving"},
    "resolver": set(),
    "security": {"dns", "serving"},
    "serving": set(),
    # `releases` because certificate collection keeps every version a retained
    # release still names.
    "tls": {
        "deployments",
        "dns",
        "edges",
        "origins",
        "releases",
        "serving",
        "workflows",
    },
    "workflows": set(),
}

# Policy composition is independent of implementation dependencies: serving
# composes the individual contracts and dns consumes it.
ALLOWED_CONTRACT_DEPENDENCIES = {
    "backup": set(),
    "origins": set(),
    "cache": {"http"},
    "compression": set(),
    "deployments": {"http"},
    "diagnostics": set(),
    "dns": {"cache", "compression", "http", "security", "serving", "tls"},
    "edges": {"http", "tls"},
    "http": set(),
    "maintenance": set(),
    "releases": {"serving"},
    "security": set(),
    "serving": {"cache", "compression", "http", "security", "tls"},
    "tls": {"http"},
    "workflows": set(),
}


def test_a_capability_contract_never_imports_an_implementation():
    """The layer rule, and the only reason two graphs are safe to have.

    A `contract` module may name `core` and another capability's `contract`; one
    that reaches for a service, adapter, `domain` or `ports` module breaks the
    order between the two graphs.
    """
    prefix = "blitzecdn.capabilities."
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _contract_files()
        for imported in sorted(_imports(path))
        if imported.startswith(prefix) and not _is_contract_import(imported)
    ]
    assert offenders == []


def test_no_strategy_mode_or_option_becomes_a_top_level_capability():
    """The fundamental rule, checked by name.

    A top-level package under `capabilities/` is a product capability. gzip and
    Brotli are strategies of compression, HTTP/1-3 versions of one protocol, Under
    Attack Mode a mode of security, certificate issuance and the Automatic SSL/TLS
    scan parts of TLS.
    """
    packages = {
        path.name
        for path in _CAPABILITIES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    offenders = [
        f"capabilities/{name} belongs inside the {owner} capability"
        for name in sorted(packages)
        for owner in [_STRATEGIES_OWNED_BY_A_CAPABILITY.get(name)]
        if owner is not None
    ]
    assert offenders == []


def test_contract_dependencies_match_what_is_declared():
    assert _contract_graph() == ALLOWED_CONTRACT_DEPENDENCIES


def test_the_capability_contract_graph_is_acyclic():
    _assert_acyclic(_contract_graph(), "capability contract")


def _graphs() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The implementation graph and the contract graph, in that order.

    An import of `blitzecdn.capabilities.<other>.contract` is a contract edge;
    everything else is an implementation edge.
    """
    prefix = "blitzecdn.capabilities."
    implementation: dict[str, set[str]] = {
        name: set() for name in ALLOWED_CAPABILITY_DEPENDENCIES
    }
    contract: dict[str, set[str]] = {
        name: set() for name in ALLOWED_CONTRACT_DEPENDENCIES
    }
    for path in _capability_files():
        owner = _capability_name(path)
        if owner not in implementation:
            continue
        for imported in _imports(path):
            if not imported.startswith(prefix):
                continue
            depends_on = imported.removeprefix(prefix).split(".")[0]
            # Any form counts, `from blitzecdn.capabilities.x import y` included:
            # importing another capability's package is depending on it.
            if depends_on == owner:
                continue
            target = contract if _is_contract_import(imported) else implementation
            target[owner].add(depends_on)
    return implementation, contract


def _capability_graph() -> dict[str, set[str]]:
    return _graphs()[0]


def _contract_graph() -> dict[str, set[str]]:
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


def test_every_capability_package_is_in_the_declared_graph():
    packages = {
        path.name
        for path in _CAPABILITIES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert packages == set(ALLOWED_CAPABILITY_DEPENDENCIES)


def test_capability_dependencies_match_what_is_declared():
    assert _capability_graph() == ALLOWED_CAPABILITY_DEPENDENCIES


def test_the_capability_dependency_graph_is_acyclic():
    """A cycle between capabilities is a modular monolith turning back into a ball."""
    _assert_acyclic(_capability_graph(), "capability dependency")


def test_no_capability_port_declares_another_capability_s_playbook():
    """The rule that keeps the graph a DAG rather than merely making it one.

    A port belongs to whoever calls it. A port describing another capability's run
    forces that capability to import this one, which is how cycles appear.
    """
    # Each edge playbook port belongs to the capability that runs that play.
    owners: dict[str, str | None] = {
        "run_cache_purge": "cache",
        "run_stats": "cache",
        "run_origin_check": "origins",
        "run_decommission": "edges",
    }
    offenders = [
        f"{path.relative_to(_SOURCE)} declares {node.name}, which belongs to {owner}"
        for path in _CAPABILITIES.glob("*/ports.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        for owner in [owners.get(node.name, _capability_name(path))]
        if owner != _capability_name(path)
    ]
    assert offenders == []


def _feature_modules() -> list[Path]:
    # Recursive: a nested slice such as `http/http3` declares its own feature.
    return sorted(_CAPABILITIES.rglob("feature.py"))


def _module_name(path: Path) -> str:
    return "blitzecdn." + path.relative_to(_SOURCE).with_suffix("").as_posix().replace(
        "/", "."
    )


def test_every_capability_declares_itself_through_a_feature_module():
    """A capability the registry has never heard of is a capability nothing runs.

    Both directions: a capability without a `feature.py` contributes nothing, and
    a `feature.py` missing from `composition.BUILTIN_FEATURES` is never imported.
    Only a capability that is a contract and domain alone may skip it.
    """
    capabilities = {
        path.name
        for path in _CAPABILITIES.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    modules = _feature_modules()
    assert set(BUILTIN_FEATURES) == {_module_name(path) for path in modules}
    declared = {
        path.relative_to(_CAPABILITIES).parts[0]
        for path in modules
        if path.is_relative_to(_CAPABILITIES)
    }
    offenders = [
        f"{name} declares no feature but holds {path.relative_to(_CAPABILITIES)}"
        for name in sorted(capabilities - declared)
        for path in sorted((_CAPABILITIES / name).rglob("*.py"))
        if path.name != "__init__.py"
        and not {"contract", "domain"}.intersection(
            part.removesuffix(".py") for part in path.parts
        )
    ]
    assert offenders == []


def test_a_capability_reads_and_writes_only_its_own_tables():
    """A table belongs to the capability whose store reads it.

    Cross-capability table access is invisible at the seam and freezes another
    capability's schema. Release pruning asks through
    `releases.ports.ReleaseReferences` instead, bound to the deployments store by
    the composition root. `core.persistence.tables` is exempt: it holds the base
    and the tables no capability owns.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _capability_files()
        for imported in sorted(_imports(path))
        if imported.endswith(".tables")
        and imported.startswith("blitzecdn.capabilities.")
        and not imported.startswith(f"blitzecdn.capabilities.{_capability_name(path)}.")
    ]
    assert offenders == []


def test_the_compiler_accepts_only_captured_values():
    """Import rules alone cannot catch I/O hidden behind injected callbacks."""
    import inspect

    from blitzecdn.capabilities.releases.service.compiler import compile_release

    parameters = inspect.signature(compile_release).parameters
    assert tuple(parameters) == ("compilation",)
    assert parameters["compilation"].annotation == "Compilation"


#: The modules that start a process, and the one container each may build.
_PROCESS_ENTRY_POINTS = {
    "entrypoints/api/app.py": "api_container",
    "entrypoints/cli/common.py": "cli_container",
    "entrypoints/worker.py": "worker_container",
    "entrypoints/install_handoff.py": "handoff_container",
}

#: What of composition each delivery layer may name. Services arrive by type
#: from the process's container, so only the CLI's schema maintenance, which
#: runs before any service exists.
_DELIVERY_CONTAINERS: dict[str, set[str]] = {
    "api": set(),
    "cli": {"migrate_database"},
}


def _names_from_composition(path: Path) -> set[str]:
    """Every name a module imports from the composition package, deferred too."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(_COMPOSITION_ROOT)
        for alias in node.names
    }


def _delivery_layer(path: Path) -> str | None:
    relative = path.relative_to(_SOURCE).parts
    if relative[0] in _DELIVERY_CONTAINERS:
        return relative[0]
    if "api" in relative[:-1]:
        return "api"
    if "cli" in relative[:-1] or relative[-1] == "cli.py":
        return "cli"
    return None


def test_each_process_builds_only_its_own_container():
    """A process's differences are which container it builds, not a flag."""
    offenders = []
    builders = set(_PROCESS_ENTRY_POINTS.values())
    for module, builder in _PROCESS_ENTRY_POINTS.items():
        built = _names_from_composition(_SOURCE / module) & builders
        if built != {builder}:
            offenders.append(f"{module} builds {sorted(built)}, not {builder}")
    assert offenders == []


def test_delivery_code_names_no_part_of_the_composition():
    """A route or command takes services by type and never names a container."""
    offenders = []
    for path in sorted(_SOURCE.rglob("*.py")):
        layer = _delivery_layer(path)
        if layer is None:
            continue
        relative = str(path.relative_to(_SOURCE))
        if relative in _PROCESS_ENTRY_POINTS:
            continue
        named = _names_from_composition(path) - _DELIVERY_CONTAINERS[layer]
        if named:
            offenders.append(f"{relative} names {sorted(named)}")
    assert offenders == []


def test_domain_and_service_code_never_reads_the_wall_clock():
    """Time is handed in: a `Clock` to a service, `now` to domain code.

    Adapters that time an external process (`core/ansible/execution.py`) and
    the ORM's fallback column defaults are outside the rule.
    """
    reads = ("datetime.now(", "datetime.utcnow(", "time.time(")
    offenders = [
        f"{path.relative_to(_SOURCE)}:{number}"
        for path in sorted(_SOURCE.rglob("*.py"))
        if {"domain", "service", "contract"} & set(path.relative_to(_SOURCE).parts)
        or path.name in {"contract.py"}
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if any(read in line for read in reads)
    ]
    assert offenders == []
