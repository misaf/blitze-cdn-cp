"""Executable boundaries for the capability-oriented modular monolith.

Two graphs, not one, and the split is the architecture.

A capability's ``policy`` module is its **contract**: pure values describing how
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
    "sqlalchemy",
    "sqlmodel",
)


#: A slice's parts are named by the directory they sit in, not by a list kept
#: here. Both of these were lists: `_DOMAIN_FILES` named `domain.py`,
#: `origins.py` and `snapshots.py`, and the adapter set named `probe.py`,
#: `preflight.py`, `desired_state.py` and a `persistence.py` beside an
#: `adapters.py` that meant the same thing. Two modules doing the same job were
#: held to different rules depending on which name somebody had thought of, and
#: the module nobody thought of was held to none — a new adapter written as
#: `renderer.py` would have been free to import the composition root.
#:
#: `domain`, `adapters`, `service` and `api` are directories in every capability
#: — `_MANDATORY_LAYER_DIRECTORIES` in `packages/test_package_layout.py`
#: refuses the file
#: spelling by name — so a module is inside the rule the moment it is put where
#: it belongs, and choosing the directory is choosing the rule. The
#: `removesuffix(".py")` below is what still admits `policy.py` and `cli.py`,
#: which are the two layers a capability may spell either way.
def _is_domain(path: Path) -> bool:
    """A capability's values: `domain/`, and the contract in `policy`."""
    parts = path.relative_to(_CAPABILITIES).parts
    return "domain" in {part.removesuffix(".py") for part in parts} or "policy" in {
        part.removesuffix(".py") for part in parts
    }


def _is_adapter(path: Path) -> bool:
    """A capability's contact with the outside: everything under `adapters/`."""
    return "adapters" in path.parts


def _is_application(path: Path) -> bool:
    """A capability's decisions: `service.py`, or everything under `service/`.

    Application policy that lives beside a service rather than inside it is
    held to the service rule, because moving code out of a service must not be
    a way to escape it. That was four file names — `service.py`, `rollback.py`,
    `reporting.py`, `validation.py` — and the three that were not `service.py`
    were only on the list because somebody put them there.
    """
    parts = path.relative_to(_CAPABILITIES).parts
    return "service" in {part.removesuffix(".py") for part in parts}


_PUBLIC_CROSS_CAPABILITY_MODULES = {
    "domain",
    "policy",
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
    way to break one, and `worker.py` and `core.persistence.schema` both use it
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


def _capability_files() -> list[Path]:
    return sorted(_CAPABILITIES.rglob("*.py"))


def _capability_name(path: Path) -> str:
    return path.relative_to(_CAPABILITIES).parts[0]


def _policy_files() -> list[Path]:
    """Every capability contract module, wherever the capability keeps it.

    Both shapes count: `tls/policy.py`, one file because the contract is one
    cohesive set of TLS values, and `serving/policy/`, a package because what the
    zone owns itself splits cleanly into headers and origin.
    """
    return sorted(
        path
        for path in _capability_files()
        if "policy" in {part.removesuffix(".py") for part in path.parts}
    )


def _is_policy_import(imported: str) -> bool:
    """Whether an import names another capability's contract rather than it."""
    parts = imported.removeprefix("blitzecdn.capabilities.").split(".")
    return len(parts) > 1 and parts[1] == "policy"


def test_legacy_layer_first_packages_have_no_source_modules():
    for package in ("domain", "application", "infrastructure"):
        assert not list((_SOURCE / package).rglob("*.py"))
    assert not list((_SOURCE / "api/routes").rglob("*.py"))


@pytest.mark.parametrize(
    "capability",
    [
        "cache",
        "compression",
        "deployments",
        "diagnostics",
        "dns",
        "edges",
        "http",
        "jobs",
        "maintenance",
        "releases",
        "security",
        "tls",
        "workflows",
    ],
)
def test_required_capability_packages_exist(capability: str):
    assert (_CAPABILITIES / capability / "__init__.py").is_file()


def test_capability_domains_are_framework_and_io_independent():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _capability_files()
        if _is_domain(path)
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                *_IO_IMPORTS,
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.composition",
                # Two packages, not seven module names. `core.runtime` is
                # everything of core's that touches the machine and
                # `core.persistence` everything that touches the database, so
                # a module added to either is refused here the day it is
                # written rather than when somebody remembers this tuple.
                "blitzecdn.core.ansible",
                "blitzecdn.core.persistence",
                "blitzecdn.core.runtime",
            ),
        )
    ]
    assert offenders == []


def test_capability_services_depend_on_contracts_not_concrete_adapters():
    forbidden = (
        *_IO_IMPORTS,
        "blitzecdn.api",
        "blitzecdn.cli",
        "blitzecdn.composition",
        "blitzecdn.core.ansible",
        "blitzecdn.core.persistence",
        "blitzecdn.core.runtime.broker",
    )
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _capability_files()
        if _is_application(path)
        for imported in sorted(_imports(path))
        if _banned(imported, forbidden) or ".adapters" in imported
    ]
    assert offenders == []


def test_capability_adapters_never_import_entry_layers_or_composition():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _capability_files()
        if _is_adapter(path)
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            (
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.composition",
                "blitzecdn.worker",
            ),
        )
    ]
    assert offenders == []


#: A capability's contact with a caller: everything under `api/` and `cli`.
#:
#: This was a set of file names — `cli.py`, `routes.py`, `readiness.py` — and a
#: name it did not list was a delivery module under no rule at all.
#: `readiness.py` was on it because somebody remembered; a slice growing a
#: `commands.py` or a `websocket.py` would not have been. It is the same defect
#: `_is_domain` and `_is_adapter` were written to fix, left in the one place
#: the fix had not reached.
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
        *(_SOURCE / "api").rglob("*.py"),
        *(_SOURCE / "cli").rglob("*.py"),
    ]


def test_entry_adapters_never_reach_persistence_or_database_directly():
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _entry_files()
        for imported in sorted(_imports(path))
        if _banned(
            imported,
            ("blitzecdn.core.persistence",),
        )
        or ".persistence" in imported
    ]
    assert offenders == []


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
            published_response = parts[1:] == ["api", "models"] and _is_entry(path)
            if (
                parts[1] not in _PUBLIC_CROSS_CAPABILITY_MODULES
                and not published_response
            ):
                offenders.append(f"{path.relative_to(_SOURCE)} imports {imported}")
    assert offenders == []


def test_the_composition_package_is_the_only_production_composition_root():
    imports = {
        name
        for module in ("roster", "infrastructure", "services", "processes")
        for name in _imports(_SOURCE / f"composition/{module}.py")
    }
    assert any(name.startswith("blitzecdn.capabilities") for name in imports)
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


def test_core_carries_no_cross_capability_application_service():
    """`core` is what a capability builds on, not a place to put a workflow.

    `MaintenanceService` lived here and orchestrated three capabilities, which
    pointed the arrow back from the foundation into the tree it supports and
    hid a genuine vertical slice where nobody would look for it. It is
    `capabilities/maintenance` now.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in (_SOURCE / "core").rglob("*.py")
        for imported in sorted(_imports(path))
        if imported.startswith("blitzecdn.capabilities.")
        and imported.endswith((".service", ".adapters"))
    ]
    assert offenders == []


def test_core_imports_no_capability():
    """The layer rule, checked rather than described.

    `core.ansible` took `EdgeStore` and projected `CdnSite`; `core.persistence`
    bundled four capability stores and held a fifth slice's table and store.
    All of it was the foundation reaching up into the tree it supports, and the
    fix was one of two: core declares the port — `ports.fleet.FleetRoster` —
    and the capability satisfies it, or the code was the capability's all along
    and moved there.

    Runtime imports only. The hookspecs annotate a parameter with `CdnSite`
    under `TYPE_CHECKING`, which is a specification naming its argument type
    and not a direction the interpreter ever takes;
    `test_the_plugin_infrastructure_depends_on_no_capability` holds that line.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in sorted((_SOURCE / "core").rglob("*.py"))
        for imported in sorted(_runtime_imports(path))
        if imported.startswith("blitzecdn.capabilities")
    ]
    assert offenders == []


def test_core_names_no_capability_even_in_a_string():
    """The same rule, asked of the text rather than of the import graph.

    `core.plugins.discovery` held `BUILTIN_PLUGINS` — eight
    `blitzecdn.capabilities.*.plugin` paths, imported by name at load time. The
    test above stayed green throughout, because a module path in a tuple is not
    an `ast.Import` and the foundation was reaching up into the tree it
    supports anyway: adding a built-in capability meant editing `core`, and
    `register_builtins` had an opinion, as a default argument, about which
    capabilities exist.

    A deferred import is still knowledge, so knowledge is what this asks about.
    The roster lives in `blitzecdn.composition` now, beside `Repository`, for the
    reason that module already gives: choosing which parts make one control
    plane is composition.

    Docstrings are exempt. `core.plugins` has to be able to say where the
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


#: Core's own floor: values, protocols, and the exception hierarchy every layer
#: may name. Membership is the directory, so a module joins the rule by being
#: put where it belongs — the bargain `_is_domain` and `_is_adapter` make for a
#: capability, and the lesson `core` learned when its infrastructure had to be
#: enumerated by module name in three separate tuples: a list covers the modules
#: somebody remembered to list, and the next one written is unguarded.
_CORE_KERNEL = ("domain", "ports", "exceptions.py")

#: What the kernel may name inside `blitzecdn`: itself. An allowlist rather than
#: a denylist here, because the direction being protected is *downward* — the
#: kernel is the bottom, so everything else in the package is above it and the
#: interesting question is what could possibly be below.
_CORE_KERNEL_IMPORTS = (
    "blitzecdn.core.domain",
    "blitzecdn.core.ports",
    "blitzecdn.core.exceptions",
)


def _core_kernel_files() -> list[Path]:
    root = _SOURCE / "core"
    return sorted(
        path
        for name in _CORE_KERNEL
        for path in ((root / name).rglob("*.py") if "." not in name else [root / name])
    )


def test_core_domain_and_ports_are_framework_and_io_independent():
    """The twin of the capability rule above, pointed at the floor it stands on.

    `core/domain/__init__.py` says of itself that nothing in it "opens a file, a
    socket, a subprocess or a database, and the layering test holds that by
    refusing this package the imports that would". That test was this one, and
    until now it did not exist: every core rule in this file guards the boundary
    *between* core and the capability tree — `test_core_imports_no_capability`
    and its two neighbours — and none of them looks inside core at all. Nothing
    refused `core.domain.audit` an import of `core.persistence.engine`.

    What rests on the property is the published SDK. `_PUBLIC_SDK_PREFIXES` in
    `published_surface.py` admits `core.domain` and `core.ports` *whole*, while
    `core.runtime` and `core.persistence` publish one named module at a time —
    `resources`, `filesystem`, `process`, `schema` — because those do I/O and
    each one is a separate promise. The comment there says the two packages are
    publishable "by construction". This is the construction: an I/O module added
    to `core/domain/` would join the public SDK the moment it was written, and
    the only thing that would have caught it was somebody remembering why the
    prefix list has two shapes in it.

    `pydantic` is allowed and is deliberately absent from `_IO_IMPORTS`: every
    value in `core/domain` is a pydantic model, and validating a value is not
    reaching for the machine. `core/exceptions.py` is in the kernel because it
    is what PLUGINS.md calls the one module every layer may name — a leaf with
    no imports at all, and a rule that let it grow one would be describing the
    package it used to be.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _core_kernel_files()
        for imported in sorted(_imports(path))
        if _banned(imported, _IO_IMPORTS)
        or (
            imported.startswith("blitzecdn")
            and not _banned(imported, _CORE_KERNEL_IMPORTS)
        )
    ]
    assert offenders == []


def test_worker_remains_an_entry_point_and_queue_direction_is_one_way():
    """The worker imports the composition root; nothing imports the worker.

    The second half is the one that matters. A capability that reached for
    `blitzecdn.worker` would make the process that *consumes* durable work a
    dependency of the code that *produces* it, and the composition root — which
    the worker builds itself from — would then be importing its own consumer.

    The queue itself is checked the same way. It is a capability now rather
    than a module under `core.runtime`, so the rule is the ordinary capability
    rule: it may not reach the composition root or an entry layer, and
    `test_capability_services_depend_on_contracts_not_concrete_adapters`
    already refuses the first. What is left here is the worker.
    """
    assert "blitzecdn.composition" in _imports(_SOURCE / "worker.py")
    offenders = [
        str(path.relative_to(_SOURCE))
        for path in _SOURCE.rglob("*.py")
        if path.name != "worker.py"
        and any(name.startswith("blitzecdn.worker") for name in _imports(path))
    ]
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
    ]


def test_a_loose_module_at_the_root_starts_a_process():
    """`blitzecdn/` holds the processes. Everything else is in a package.

    The root was answering two questions at once and answering neither.
    `bootstrap.py`, `persistence.py` and `scheduler.py` — how a control plane is
    assembled — sat beside `worker.py` and `install_handoff.py` — which
    processes exist — with nothing to say which kind a file was, so "what runs
    here" could only be answered by opening all five. The three moved into
    `composition/`.

    A process is checked rather than declared: something outside Python has to
    name the module, because that is what starting a process looks like from
    here. `dramatiq blitzecdn.worker` is in the controller's compose file and
    `python -m blitzecdn.install_handoff` is in `install.sh`, so both are
    answerable without trusting a list in this test. The host SDK runner is
    copied out of the package and launched by its script path because the host
    does not install the application. A helper dropped at the
    root is named by nothing and fails.

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
        for path in sorted(SOURCE.glob("*.py"))
        if path.name != "__init__.py"
        and f"blitzecdn.{path.stem}" not in artifacts
        and f"/{path.name}" not in artifacts
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


def test_removed_subsystems_do_not_return():
    offenders = [
        str(path.relative_to(_SOURCE))
        for path in _SOURCE.rglob("*.py")
        if "outbox" in path.read_text(encoding="utf-8").lower()
        or "ThreadBackgroundRunner" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_stored_field_has_a_writer_outside_the_capability_that_owns_it():
    """The rule this replaces, and why there is nothing left for it to guard.

    ``CdnSite.server_names`` used to be the one column with a writer outside
    its own capability: `dns` maintained it from the records routed to a site,
    through a `SiteHostnames` port, and a site write that also carried
    hostnames would silently revert a record change made between the read and
    the write. A whole test, a whole port and a projection revision existed to
    keep that one field honest.

    A derived host has no columns at all. What is asserted now is that it
    stayed that way: nothing stores a virtual host, so nothing can be a second
    writer of one.
    """
    from blitzecdn.capabilities.dns.adapters.persistence import ZoneStore
    from blitzecdn.capabilities.dns.adapters.rules import RuleStore
    from blitzecdn.capabilities.dns.domain import DomainPatch
    from blitzecdn.capabilities.serving.domain import CdnSite

    writers = {name for name in vars(ZoneStore) if not name.startswith("_")} | {
        name for name in vars(RuleStore) if not name.startswith("_")
    }
    assert not any("site" in name for name in writers), sorted(writers)

    # The mirror of the old rule, on the patch: every setting a zone stores can
    # be patched, and `server_names` is not one of them because no one sets it.
    assert "server_names" not in DomainPatch.model_fields
    assert "server_names" in CdnSite.model_fields


#: Which capability may know that another exists. Derived from the graph the code
#: actually has, and enforced both ways: an undeclared edge fails, and so does
#: a declared edge nothing uses any more.
#:
# DNS owns zones, records, rules, and their projection. Shared serving values
# belong to serving; execution modules consume those values directly.
ALLOWED_CAPABILITY_DEPENDENCIES = {
    "backup": set(),
    "origins": {"edges", "serving"},
    "cache": {"dns", "serving"},
    "compression": set(),
    "deployments": {"dns", "edges", "releases", "serving", "workflows"},
    "diagnostics": set(),
    "dns": {"serving"},
    "edges": {"serving"},
    "http": {"serving"},
    "jobs": set(),
    "maintenance": {"deployments"},
    "releases": {"dns", "edges", "serving"},
    "security": {"serving"},
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
ALLOWED_POLICY_DEPENDENCIES = {
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

    A `policy` module may name `core` and another capability's `policy`. The
    moment one reaches for a service, an adapter, a `domain` or a `ports`
    module, the contract layer stops being below the implementation layer and
    the two declared graphs stop composing into an order at all.
    """
    prefix = "blitzecdn.capabilities."
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in _policy_files()
        for imported in sorted(_imports(path))
        if imported.startswith(prefix) and not _is_policy_import(imported)
    ]
    assert offenders == []


def test_serving_owns_shared_policy_and_dns_owns_its_projection():
    """A certificate or deployment need not depend on DNS to name a host."""
    assert _policy_graph()["serving"] == {
        "cache",
        "compression",
        "http",
        "security",
        "tls",
    }
    assert _capability_graph()["serving"] == set()
    assert not (_CAPABILITIES / "dns/domain/host.py").exists()
    assert not (_CAPABILITIES / "dns/policy/__init__.py").exists()
    owned = {path.stem for path in (_CAPABILITIES / "serving/policy").glob("*.py")}
    assert owned == {"__init__", "headers", "origin", "site"}


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


def test_policy_dependencies_match_what_is_declared():
    assert _policy_graph() == ALLOWED_POLICY_DEPENDENCIES


def test_the_capability_contract_graph_is_acyclic():
    _assert_acyclic(_policy_graph(), "capability contract")


def _graphs() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The implementation graph and the contract graph, in that order.

    An import of `blitzecdn.capabilities.<other>.policy` is a *contract* edge and
    lands in the second. Everything else — the package itself, a domain module,
    a port, a service — is an implementation edge and lands in the first.
    """
    prefix = "blitzecdn.capabilities."
    implementation: dict[str, set[str]] = {
        name: set() for name in ALLOWED_CAPABILITY_DEPENDENCIES
    }
    contract: dict[str, set[str]] = {
        name: set() for name in ALLOWED_POLICY_DEPENDENCIES
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
            target = contract if _is_policy_import(imported) else implementation
            target[owner].add(depends_on)
    return implementation, contract


def _capability_graph() -> dict[str, set[str]]:
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
    declare: when it is a contract and nothing beyond its policy and domain.
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
        and not {"policy", "domain"}.intersection(
            part.removesuffix(".py") for part in path.parts
        )
    ]
    assert offenders == []


def test_no_capability_or_provider_names_the_composition_root():
    """Nothing the composition root builds may reach back for it.

    Not a service, an adapter, a builder or a feature: each receives its
    collaborators as named arguments. A module that reached for the root would be
    resolving its collaborators at call time instead of receiving them, which
    is the service-locator shape this design refuses. Not even for typing.

    A capability's *entry* modules are outside the rule for the same reason
    `cli/common.py` is: a command has to get its services from somewhere.
    """
    entry = set(_entry_files())
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in [
            *_capability_files(),
            *sorted((_SOURCE / "providers").rglob("*.py")),
        ]
        if path not in entry
        for imported in sorted(_imports(path))
        if imported.startswith(_COMPOSITION_ROOT)
    ]
    assert offenders == []


def test_a_runtime_module_imports_no_delivery_layer():
    """`runtime.py` is what the composition root imports to bind behaviour.

    Delivery modules import the composition root, so a runtime module that
    imported one would make composing the application an import cycle. A
    feature's routes and commands are declared in `feature.py`, which the
    registry loads by name instead.
    """
    delivery = ("fastapi", "typer", "blitzecdn.api", "blitzecdn.cli")
    runtimes = [
        *_CAPABILITIES.glob("*/runtime.py"),
        *(_SOURCE / "providers").glob("*/runtime.py"),
    ]
    assert runtimes
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in sorted(runtimes)
        for imported in sorted(_runtime_imports(path))
        if imported.startswith(delivery)
        or ".api" in imported
        or imported.endswith((".cli", ".feature"))
    ]
    assert offenders == []


def test_the_entry_layers_import_no_capability_at_all():
    """The point of the whole exercise, asserted where it can be checked.

    `api/app.py` used to name seventeen router modules and `cli/main.py` eleven
    command groups, so adding a capability meant editing both. They ask the plugin
    registry now, and a capability that appears in neither list is a capability that
    forgot to contribute — not one somebody forgot to wire.

    Only these two modules. `api/models.py` translates domain values into the
    published HTTP representations and has to name them; that is a
    *translation*, and it is the assembly that had to stop knowing the
    capability list.
    """
    offenders = [
        f"{path.relative_to(_SOURCE)} imports {imported}"
        for path in (_SOURCE / "api/app.py", _SOURCE / "cli/main.py")
        for imported in sorted(_imports(path))
        if imported.startswith("blitzecdn.capabilities")
    ]
    assert offenders == []


def test_the_plugin_infrastructure_depends_on_no_capability():
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
        if imported.startswith(("blitzecdn.capabilities", _COMPOSITION_ROOT))
    ]
    assert offenders == []


#: Modules whose whole value is that they are functions of their arguments.
#:
#: A pure module can be run twice and compared, run in a test with nothing
#: installed, and reasoned about without knowing what else has happened. Each
#: of those properties is lost silently — a `datetime.now()` deep in a helper
#: does not fail anything, it just makes the output stop being reproducible —
#: so the property is asserted rather than left to review.
_PURE_MODULES = ("capabilities/releases/service/compiler.py",)

#: What a pure module may not reach for. Beyond the I/O imports every domain
#: module is already held to: a clock, a random source, the environment, and
#: the filesystem.
_IMPURE_SOURCES = (
    "datetime",
    "time",
    "random",
    "secrets",
    "os",
    "pathlib",
    "socket",
    "urllib",
    "httpx",
    "requests",
)


def test_the_compiler_is_a_function_of_its_arguments():
    """Compiling a release reads no clock, no environment and no disk.

    This is what makes a release content-addressed rather than merely hashed:
    two compilations of the same canonical state, on installations with the
    same capabilities, aimed at the same edges, produce the same digest. A
    single `datetime.now()` anywhere under here would break that without
    breaking anything that fails loudly — every release would simply become a
    new release, and every "has anything changed" comparison would answer yes.
    """
    offenders = [
        f"{name} imports {imported}"
        for name in _PURE_MODULES
        for imported in sorted(_imports(_SOURCE / name))
        if _banned(imported, (*_IO_IMPORTS, *_IMPURE_SOURCES))
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
    "api/app.py": "build_api",
    "cli/common.py": "build_cli",
    "worker.py": "build_worker",
    "install_handoff.py": "build_install_handoff",
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
        if {"domain", "service", "policy"} & set(path.relative_to(_SOURCE).parts)
        or path.name in {"policy.py"}
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if any(read in line for read in reads)
    ]
    assert offenders == []
