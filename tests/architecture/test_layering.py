"""Executable boundaries for the capability-oriented modular monolith.

Two graphs, not one, and the split is the architecture.

A capability's ``contract`` module holds pure values describing how
the capability is configured, importing nothing but ``core`` and another
capability's contract. Its other modules are its **implementation**: the
services, adapters, routers and commands that act on those values.

``dns`` composes every capability's contract into one flat virtual host, and
most capabilities' implementations consume ``CdnSite``. Counting both as one
kind of edge would make that a cycle and force the contracts back into
``dns`` — which is exactly the "one capability owns every setting" shape this
replaced. So contract edges and implementation edges are declared and checked
separately, and the layer rule that keeps the whole thing a DAG is asserted
directly: **a contract never imports an implementation.**

    core  <  capability contracts  <  capability implementations

Which module may import which is checked by import-linter: the contracts in
`.importlinter`, run by `just lint`. What is left here is what an import graph
cannot express — names and strings, files that must or must not exist, the
declared capability graphs (an unused declared edge fails too), allowlists of
which submodules another capability may name, and import cycles at module
scope only.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
from paths import CORE_ANSIBLE, REPO_ROOT, SOURCE

from blitzecdn.composition import BUILTIN_FEATURES

_SOURCE = SOURCE
_COMPOSITION_ROOT = "blitzecdn.composition"
_CAPABILITIES = _SOURCE / "capabilities"


#: A slice's parts are named by the directory they sit in, not by a list kept
#: here. Both of these were lists: `_DOMAIN_FILES` named `domain.py`,
#: `origins.py` and `snapshots.py`, and the adapter set named `probe.py`,
#: `preflight.py`, `desired_state.py` and a `persistence.py` beside an
#: `adapters.py` that meant the same thing. Two modules doing the same job were
#: held to different rules depending on which name somebody had thought of, and
#: the module nobody thought of was held to none — a new adapter written as
#: `renderer.py` would have been free to import the composition root.
#:
#: `domain`, `adapters`, `service` and `api` are directories, so a module is
#: inside the rule the moment it is put where it belongs, and choosing the
#: directory is choosing the rule. The
#: `removesuffix(".py")` below is what still admits `contract.py` and `cli.py`,
#: which are the two layers a capability may spell either way.
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
    "reporting",
}

#: Names that must never become a top-level capability package. Each is a
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


def _module_scope_imports(path: Path) -> set[str]:
    """Only the imports the interpreter runs while importing this module.

    An import inside a function body executes when that function is *called*,
    so it cannot produce a circular import at load time — it is the documented
    way to break one, and `worker.py` and `core.persistence.schema` both use it
    deliberately. Counting it as an edge would make the cycle test refuse the
    remedy for the problem it exists to detect.

    So this drops `TYPE_CHECKING` blocks and anything nested in a `def`,
    `async def` or `class` body. import-linter cannot ask this question: its
    graph counts a deferred import like any other.
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
    """Every capability contract module, wherever the capability keeps it.

    Both shapes count: `tls/contract.py`, one file because the contract is one
    cohesive set of TLS values, and `serving/contract/`, a package because what the
    zone owns itself splits cleanly into headers and origin.
    """
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
#: This was a set of file names — `cli.py`, `routes.py`, `readiness.py` — and a
#: name it did not list was a delivery module under no rule at all.
#: `readiness.py` was on it because somebody remembered; a slice growing a
#: `commands.py` or a `websocket.py` would not have been. It is the same defect
#: the positional `domain/` and `adapters/` rules were written to fix, left in
#: the one place the fix had not reached.
#:
#: Both are positional now, like `adapters/`. `cli` was still a name — and so
#: was exact — until `sites/cli.py` became `sites/cli/`, at which point matching
#: on the name alone would have taken seven modules of commands out of the
#: entry rule on the day they were split out of a module that was in it. A
#: capability spells this layer either way, exactly as it spells `domain`.
#:
#: `api/` and `cli` stay where they are rather than moving under `adapters/`,
#: which would make membership positional for both. They are not private the
#: way an adapter is — `capabilities.deployments.api.models` is a published
#: contract an installed package may name, and
#: `test_entry_layers_cannot_reach_private_control_plane_adapters_or_stores`
#: exists to keep `adapters/` unreachable. Filing the published surface under
#: the private one would put those two rules in each other's way.
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

#: `blitzecdn.composition` is the composition root for the control plane:
#: `control_plane` wires the services, `repository` chooses which capability
#: stores sit on one SQLite file, `scheduler` chooses which contributed jobs
#: get triggers. Membership is the directory, so a fourth module added there is
#: inside the allowance by being put where it belongs.
#:
#: `acme_hook` is the one composition root outside it, and deliberate: certbot
#: runs it as a one-shot subprocess with no control plane in the picture, and it
#: builds the two adapters an HTTP-01 challenge needs and nothing else. It is
#: named here — a name, because it is the exception — so a second one does not
#: appear quietly beside it.
_COMPOSITION_EXCEPTIONS = {"acme_hook.py"}


def _is_composition(path: Path) -> bool:
    return (
        _SOURCE / "composition" in path.parents or path.name in _COMPOSITION_EXCEPTIONS
    )


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

    `core.features.discovery` held `BUILTIN_PLUGINS` — eight
    `blitzecdn.capabilities.*.feature` paths, imported by name at load time. The
    test above stayed green throughout, because a module path in a tuple is not
    an `ast.Import` and the foundation was reaching up into the tree it
    supports anyway: adding a built-in capability meant editing `core`, and
    `register_builtins` had an opinion, as a default argument, about which
    capabilities exist.

    A deferred import is still knowledge, so knowledge is what this asks about.
    The roster lives in `blitzecdn.composition` now, beside `Repository`, for the
    reason that module already gives: choosing which parts make one control
    plane is composition.

    Docstrings are exempt. `core.features` has to be able to say where the
    roster went, and prose pointing *at* the composition root is the opposite
    of core holding the list.
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
    """`entrypoints/` and `host/` hold the processes. Everything else is in a
    package, and the package root holds no loose module at all.

    The root was answering two questions at once and answering neither.
    `bootstrap.py`, `persistence.py` and `scheduler.py` — how a control plane is
    assembled — sat beside `worker.py` and `install_handoff.py` — which
    processes exist — with nothing to say which kind a file was, so "what runs
    here" could only be answered by opening all five. The three moved into
    `composition/`.

    A process is checked rather than declared: something outside Python has to
    name the module, because that is what starting a process looks like from
    here. `python -m blitzecdn.entrypoints.worker` is in the controller's
    compose file and `python -m blitzecdn.entrypoints.install_handoff` is in
    `install.sh`, so both are answerable without trusting a list in this test.
    The two `host/` scripts are launched by their script paths, because the
    host does not install the application. A helper dropped beside them, or at
    the package root, is named by nothing and fails.

    `api/` and `cli/` are processes too and are packages, because each has a
    surface — dependencies, models, the root Typer app — that a single module
    would not hold. The rule is about loose modules, which is where a helper
    lands when nobody decides where it goes.
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


#: Which capability may know that another exists. Derived from the graph the code
#: actually has, and enforced both ways: an undeclared edge fails, and so does
#: a declared edge nothing uses any more.
#:
# DNS owns zones, records, rules, and their projection. Shared serving values
# belong to serving; execution modules consume those values directly. A
# capability whose contract a zone carries depends on dns for the commands
# that edit it under `blitzecdn domain`.
ALLOWED_CAPABILITY_DEPENDENCIES = {
    "backup": set(),
    "origins": {"edges", "serving"},
    "cache": {"dns", "serving"},
    "compression": {"dns"},
    "deployments": {"dns", "edges", "releases", "serving", "workflows"},
    "diagnostics": set(),
    "dns": {"serving"},
    "edges": {"serving"},
    "http": {"dns", "serving"},
    "jobs": set(),
    "maintenance": {"deployments"},
    "releases": {"dns", "edges", "serving"},
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

# Policy composition is independent of implementation dependencies. Serving
# composes the individual contracts; DNS consumes it and validates flat patches
# with the individual field types until the nested policy step replaces them.
ALLOWED_CONTRACT_DEPENDENCIES = {
    "backup": set(),
    "origins": {"http", "tls"},
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

    A `contract` module may name `core` and another capability's `contract`. The
    moment one reaches for a service, an adapter, a `domain` or a `ports`
    module, the contract layer stops being below the implementation layer and
    the two declared graphs stop composing into an order at all.
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
    Brotli are strategies of compression; HTTP/1, /2 and /3 are versions of one
    protocol; Under Attack Mode is a mode of security; certificate issuance and
    the Automatic SSL/TLS scan are parts of TLS. Each of these has been, or
    could plausibly become, a package of its own — `http3` and `certificates`
    both were — and each time the result is a capability list that answers "what
    settings exist" instead of "what can this product do".
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

    An import of `blitzecdn.capabilities.<other>.contract` is a *contract* edge and
    lands in the second. Everything else — the package itself, a domain module,
    a port, a service — is an implementation edge and lands in the first.
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
    """A cycle between capabilities is a modular monolith turning back into a ball.

    There were four, and every one of them ran through a single fat port:
    `DeploymentRunner` used to declare the purge, stats, origin-check and
    decommission plays as well as the deploy, so `cache` and `edges` had to
    import the deployment package to reach their own playbook, while
    `deployments` imported `cache.domain` for the purge entries it named and
    `certificates.ports` for two certificate paths. Each capability declares the
    slice it uses now, the composition root is the only place that knows one
    adapter satisfies all of them, and the graph is a DAG.
    """
    _assert_acyclic(_capability_graph(), "capability dependency")


def test_no_capability_port_declares_another_capability_s_playbook():
    """The rule that keeps the graph a DAG rather than merely making it one.

    A port belongs to whoever calls it. The moment one capability's port module
    describes a run another capability performs, that other capability has to import
    this one to type its own collaborator — which is exactly how the cycles got
    there.
    """
    # `run_cache_purge`, `run_stats` and `run_origin_check` belong to no
    # capability at all any more — they left with the distributions that run
    # those plays, and a core `ports.py` naming one would be core declaring an
    # optional capability's operation. `None` says exactly that: no capability
    # may declare it, not even the one it used to live beside.
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
    return sorted(
        [
            *_CAPABILITIES.glob("*/feature.py"),
            *(_SOURCE / "providers").glob("*/feature.py"),
        ]
    )


def _module_name(path: Path) -> str:
    return "blitzecdn." + path.relative_to(_SOURCE).with_suffix("").as_posix().replace(
        "/", "."
    )


def test_every_capability_declares_itself_through_a_feature_module():
    """A capability the registry has never heard of is a capability nothing runs.

    Both directions: a capability without a `feature.py` contributes nothing,
    and a `feature.py` missing from `composition.BUILTIN_FEATURES` is never
    imported — either way the routes and commands quietly are not there.

    A capability may skip declaring a feature only when it has nothing to
    declare: when it is a contract and nothing beyond its contract and domain.
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

    Cross-module table access is the thing that makes a modular monolith stop
    being modular: it is invisible at the seam, it survives every refactor of
    the port between the two, and it turns another capability's schema into
    something you cannot change without breaking a caller you did not know you
    had. Release pruning is the standing temptation — it has to know which
    releases a deployment still names — and it asks through
    `releases.ports.ReleaseReferences` instead, which the composition root
    binds to the deployments store.

    `core.persistence.tables` is exempt: it holds the declarative base and the
    two tables that belong to no capability, and every table module imports it.
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
    "entrypoints/api/app.py": "build_api",
    "entrypoints/cli/common.py": "build_cli",
    "entrypoints/worker.py": "build_worker",
    "entrypoints/install_handoff.py": "build_install_handoff",
}

#: What of composition each delivery layer may name: its own container, and for
#: the CLI the schema maintenance that runs before any service exists.
_DELIVERY_CONTAINERS = {
    "api": {"ApiServices"},
    "cli": {"CliServices", "migrate_database"},
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


def test_delivery_code_reaches_only_its_own_process_container():
    """A route cannot reach a service the API was not given, nor a command one
    the CLI was not given: neither may name the service graph or another
    process's container."""
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
