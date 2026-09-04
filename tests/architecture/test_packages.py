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
from paths import CORE_DOCKER, REPO_ROOT, SOURCE, optional_packages

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
    assert all(
        module.startswith("blitzecdn.capabilities.") for module in BUILTIN_PLUGINS
    )


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
def test_an_optional_package_depends_on_the_control_plane_and_workspace_only(
    package: Path,
):
    """Every dependency points inward, with an explicit compatibility range.

    `blitzecdn` first and always. The upper bound is not decoration:
    `HOOK_API_VERSION` may only move in a major, and a plugin written against
    v1 that silently installed beside a v2 control plane would be refused at
    registration with a message about a hook contract rather than about the
    dependency that allowed it.

    Anything after it must be another distribution in this workspace, pinned
    the same way. That is the *declared* form of a cross-package edge, and it
    is the only form allowed — see
    `test_optional_packages_depend_on_each_other_only_when_they_say_so`, which
    refuses the undeclared one. Nothing else may appear at all: a third-party
    runtime dependency in a capability wheel is a dependency of the whole
    installation, added where nobody would look for it.
    """
    project = _manifest(package)["project"]
    names = [
        requirement.split(">")[0].split("[")[0].strip()
        for requirement in project["dependencies"]
    ]
    assert names[0] == "blitzecdn"
    workspace = {path.name for path in _packages()}
    assert set(names[1:]) <= workspace, (
        f"{package.name} depends on {set(names[1:]) - workspace}, which is "
        "neither the control plane nor a distribution in this workspace"
    )
    for requirement in project["dependencies"]:
        assert "<4" in requirement, requirement


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_optional_packages_depend_on_each_other_only_when_they_say_so(package: Path):
    """An optional-to-optional edge is allowed, and only in its declared form.

    Avoided rather than forbidden outright. A package that genuinely needs
    another declares it as a real dependency in `pyproject.toml`, and pip then
    installs both. What is refused is the *undeclared* form — an import that
    happens to work because both are installed today — because that is what
    makes detaching one break the other with an ImportError nothing predicted.

    There is exactly one such edge today: `blitzecdn-certificates` runs
    `blitzecdn-origins`' play for the Automatic SSL/TLS scan, which probes each
    candidate origin from every edge over its current transport and again under
    Full (strict). It is a real requirement — without that answer the scan
    cannot recommend anything — so it is written down rather than worked
    around.
    """
    declared = {
        requirement.split(">")[0].split("[")[0].strip().replace("-", "_")
        for requirement in _manifest(package)["project"]["dependencies"]
    }
    others = _optional_import_roots() - {_import_package(package)} - declared
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
    # How a wheel finds its own roles, plays and templates on disk, and which
    # version of itself it is. Published because every capability needs both
    # and each one used to answer them itself: eight copies of one guard, two
    # of which were never written, and eleven `__version__` literals that a
    # release could leave behind.
    "blitzecdn.core.resources",
    "blitzecdn.core.ports",
    "blitzecdn.core.process",
    "blitzecdn.api.dependencies",
    "blitzecdn.api.operations",
    "blitzecdn.api.requests",
    "blitzecdn.cli.common",
)

#: A capability contract another capability owns. Allowed, and named one by one
#: rather than by a wildcard over `blitzecdn.capabilities.*`: `CdnSite` and
#: `HttpScheme` are contracts every capability already consumes, while a
#: capability's `service` or `adapters` module is not something an installed
#: package may reach into.
_PUBLIC_CAPABILITY_MODULES = (
    "blitzecdn.capabilities.sites",
    "blitzecdn.capabilities.cache.policy",
    "blitzecdn.capabilities.http.policy",
    "blitzecdn.capabilities.dns.domain",
    "blitzecdn.capabilities.dns.ports",
    "blitzecdn.capabilities.deployments.domain",
    "blitzecdn.capabilities.deployments.ports",
    "blitzecdn.capabilities.edges.origins",
    "blitzecdn.capabilities.edges.ports",
    "blitzecdn.capabilities.tls.policy",
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


# --- the layout inside a package is a vocabulary, not a habit ---------------


#: The modules a package's Python is organised into. Held to a closed set for
#: the same reason `ALLOWED_CAPABILITY_DEPENDENCIES` is one: ten packages that
#: converged on a shape by imitation give the eleventh author ten examples and
#: no rule, and the shape is what makes a capability readable without reading
#: it — `plugin.py` is what it contributes, `composition.py` is how it is
#: built, `ports.py` is what it calls. A package uses as few of these as it
#: needs; `blitzecdn-compression` is a `plugin.py` and nothing else.
#:
#: Growing the set is allowed and is a decision: add the name here with the
#: sentence saying what belongs in it, and to the canonical package in
#: PLUGINS.md. What this refuses is the name that arrives without either.
_PACKAGE_MODULES = {
    "__init__.py",
    "plugin.py",  # metadata and the hooks it contributes through
    "composition.py",  # builds its service from what the platform publishes
    "config.py",  # its own settings, read from its CapabilityConfig
    "domain.py",  # pure rules
    "ports.py",  # the narrow Protocols this capability calls
    "service.py",  # the capability's behaviour
    "reporting.py",  # application policy beside the service, held to its rule
    "cli.py",  # its command groups
    # A capability small enough for one adapter *module* rather than an
    # `adapters/` package is still an adapter, and `test_layering` already
    # holds both spellings to the adapter rule.
    "adapters.py",
    "preflight.py",
}

#: `api/` holds the routes and the package's own operational shapes, and
#: nothing else. A package whose HTTP surface needs a third module is
#: describing something that is not an HTTP adapter.
_API_MODULES = {"__init__.py", "models.py", "routes.py"}

#: Directories whose *contents* are named after what they implement rather
#: than after a layer — `archive.py`, `playbooks.py` — because that is the
#: point of an adapter. Flat: an adapter package with a package inside it is a
#: layer split this size of code does not have.
_FREE_FORM_DIRECTORIES = {"adapters"}

#: What a package ships for something other than Python to read. `ansible/`
#: carries one module — the `importlib.resources` anchor — and its roles and
#: plays; `nginx/` carries templates only. A `.py` file deeper in either is a
#: capability putting logic where no test looks for it.
_ANSIBLE_DIRECTORIES = {"roles", "playbooks"}
_NGINX_SUFFIX = ".conf.j2"

#: Modules outside the vocabulary, by distribution, each with the reason.
#: `acme_hook` is a process entry point rather than part of the capability's
#: composition: certbot execs it as a one-shot subprocess with no control
#: plane in the picture, which is why `test_layering` also names it beside
#: `bootstrap.py` as a second composition root.
_DECLARED_EXTRA_MODULES = {"blitzecdn-certificates": {"acme_hook.py"}}


def _module_offence(parts: tuple[str, ...], *, nested: bool) -> str | None:
    head, *rest = parts
    if not rest:
        return (
            None if head in _PACKAGE_MODULES else f"{head} is not a documented module"
        )
    if head == "ansible":
        if rest == ["__init__.py"]:
            return None
        return "ansible/ ships roles and plays; its only module is the anchor"
    if head == "nginx":
        return "nginx/ ships templates, not Python"
    if head == "api":
        if len(rest) == 1 and rest[0] in _API_MODULES:
            return None
        return f"api/{'/'.join(rest)} is not a router, a version or the shared models"
    if head in _FREE_FORM_DIRECTORIES:
        return None if len(rest) == 1 else f"{head}/ is flat"
    if nested:
        return f"{head}/ nests a capability two levels deep"
    return _module_offence(tuple(rest), nested=True)


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_a_package_organises_its_python_into_the_documented_modules(package: Path):
    """The canonical package in PLUGINS.md, checked against the real tree.

    A capability may nest *one* level — `blitzecdn-certificates` holds
    `certificates/` and `automatic_ssl/`, which are two jobs on one capability
    rather than two capabilities — and each nested directory is held to the
    same vocabulary. A second level is a package that wants to be two
    distributions, and it says so here rather than three refactors later.
    """
    root = package / "src" / _import_package(package)
    allowed_extra = _DECLARED_EXTRA_MODULES.get(package.name, set())
    offenders = [
        f"{path.relative_to(root)}: {offence}"
        for path in _source_files(package)
        for parts in (path.relative_to(root).parts,)
        if not (len(parts) == 1 and parts[0] in allowed_extra)
        for offence in (_module_offence(parts, nested=False),)
        if offence
    ]
    assert offenders == []


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_a_package_ships_its_ansible_and_its_templates_where_the_layout_says(
    package: Path,
):
    """The two directories the control plane locates by path, not by import.

    Core composes `roles_path` from what a contribution answers with and hands
    `templates_path` to the renderer, so a file in the wrong place here is not
    a tidiness question: it is a role Ansible will not resolve or a fragment
    the renderer will not find, and both fail on an edge rather than in CI.
    """
    root = package / "src" / _import_package(package)
    offenders = []
    ansible = root / "ansible"
    if ansible.is_dir():
        offenders += [
            f"ansible/{entry.name} is neither the anchor, roles/ nor playbooks/"
            for entry in sorted(ansible.iterdir())
            if entry.name not in {"__init__.py", "__pycache__"}
            and not (entry.is_dir() and entry.name in _ANSIBLE_DIRECTORIES)
        ]
    nginx = root / "nginx"
    if nginx.is_dir():
        offenders += [
            f"nginx/{path.relative_to(nginx)} is not a {_NGINX_SUFFIX} template"
            for path in sorted(nginx.rglob("*"))
            if path.is_file()
            and "__pycache__" not in path.parts
            and not path.name.endswith(_NGINX_SUFFIX)
        ]
    assert offenders == []


def test_the_documented_layout_and_the_enforced_one_are_the_same():
    """PLUGINS.md carries the vocabulary; this file refuses departures from it.

    Two registers of one rule drift, and the one that drifts is the prose,
    because nothing fails when it does. A name enforced here and absent from
    the canonical package is a rule a capability author cannot read.
    """
    documented = (REPO_ROOT / "PLUGINS.md").read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _PACKAGE_MODULES | _API_MODULES | _FREE_FORM_DIRECTORIES
        if name != "__init__.py" and name not in documented
    )
    assert missing == []


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


def _dynamic_package_names(path: Path) -> set[str]:
    """Optional distributions a module names as a *string* rather than imports.

    `import_module("blitzecdn_cache...")` and `find_spec("blitzecdn_cache")`
    are imports that `ast.Import` cannot see, which is how the rule above came
    to be satisfied by fifty-six tests that were certificate tests all along:
    each was written as a conditional import at module scope and then held in
    a hand-maintained set of names for the fixtures to skip.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = (
            callee.attr
            if isinstance(callee, ast.Attribute)
            else getattr(callee, "id", None)
        )
        if name not in {"import_module", "find_spec"}:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.add(argument.value.split(".")[0])
    return found


def test_the_control_plane_suite_does_not_reach_a_package_by_name_either():
    """The same rule, against the spelling that evades the one above.

    A conditional import is still core knowing an implementation, and it is
    strictly worse than a plain one: the module imports either way, so the
    dependency shows up as a test skipped in the core-only run rather than as
    an error anyone reads. The one permitted use is the fixture that skips a
    *cross-package rendering* contract — an assertion about a fragment another
    distribution contributes to core's own template, which has no single owner
    to move to — and it is allowed by file, not by test name.
    """
    optional = _optional_import_roots()
    allowed = {
        "test_packages.py",
        "test_lifecycle.py",
        # `skip_tests_a_detached_capability_cannot_answer` lives here.
        "control_plane_fixtures.py",
    }
    offenders = [
        f"{path.name} names {named}"
        for path in sorted(Path(__file__).parents[1].rglob("*.py"))
        if path.name not in allowed
        for named in sorted(_dynamic_package_names(path))
        if named in optional
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
    assert "blitzecdn.capabilities.http.plugin" in BUILTIN_PLUGINS


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
    from blitzecdn.capabilities.http.policy import ProtocolPolicy
    from blitzecdn.capabilities.sites.domain import CdnSite

    assert "http3_enabled" in ProtocolPolicy.model_fields
    assert "http3_enabled" in CdnSite.model_fields
    assert ProtocolPolicy.__module__.startswith("blitzecdn.capabilities.http")


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

    `CdnSite` composes them by inheritance into the flat shape the published
    schemas, the persisted policy JSON and the deployment snapshots consume.
    Moving one into the package would make that shape depend on what is
    installed, and a stored site asking for a country would stop loading on a
    controller that had detached it.
    """
    from blitzecdn.capabilities.security.policy import SiteFirewall
    from blitzecdn.capabilities.sites.domain import CdnSite
    from blitzecdn.capabilities.sites.policy.headers import SiteVisitorHeaders

    assert {"allowed_countries", "denied_countries"} <= set(SiteFirewall.model_fields)
    assert "ip_country" in SiteVisitorHeaders.model_fields
    assert {"firewall", "visitor_headers"} <= set(CdnSite.model_fields)
    assert SiteFirewall.__module__.startswith("blitzecdn.capabilities.security")
    assert SiteVisitorHeaders.__module__.startswith("blitzecdn.capabilities.sites")


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
    "capabilities/sites/policy/headers.py",
}


def test_the_country_settings_derive_their_token_generically_in_core():
    """The derivation is core's, and it is not written as a GeoIP special case.

    `capability_requirements` maps every stable setting onto the token it needs
    the same way, so `geoip` arrives by the same path as `compression` or
    `http3` — declared by the contract that wants it, merged by `sites` with no
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


def test_the_composition_names_no_capability_token_at_all():
    """The property the whitelist above is only the GeoIP half of.

    `sites/domain.py` composes every contract's requirements and may not name
    one of them. It named six, in an `if` chain that restated each capability's
    own rule beside it — two places to edit, and nothing to catch the day they
    disagreed.
    """
    document = (SOURCE / "capabilities/sites/domain.py").read_text(encoding="utf-8")
    tokens = ("geoip", "cache", "compression", "http3", "certificates", "security")
    offenders = [
        f"sites/domain.py names the {node.value} token"
        for node in ast.walk(ast.parse(document))
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
    assert "<4" in declared

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


# --- the firewall belongs to the security capability, not to core -----------


#: The six kinds of rule a site firewall carries, and the vocabulary they are
#: validated against. `SiteFirewall` is the source of the first six, so a
#: seventh rule kind lands here without this list being edited.
def _firewall_rule_kinds() -> frozenset[str]:
    from blitzecdn.capabilities.security.policy import SiteFirewall

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
#: * `api/models.py` holds the published *resource* shapes, which restate the
#:   fields rather than re-export the contract, because what a client is
#:   shown is a decision separate from what a policy happens to hold;
#: * `dns/cli.py` carries `blitzecdn record firewall`, because a record patch
#:   is the DNS capability's surface and `dns -> security` is a declared
#:   contract edge in `ALLOWED_POLICY_DEPENDENCIES`;
#: `sites/domain.py` used to be a fourth entry, permitted to name the two
#: *country* settings so it could derive the `geoip` token. It derives nothing
#: now — `SecurityPolicy` declares the token beside the rule that needs it — so
#: the exception is gone rather than documented, which is the outcome worth
#: having for a list whose whole purpose is to stay short.
_FIREWALL_AWARE_MODULES: dict[str, frozenset[str] | None] = {
    "capabilities/security/policy.py": None,
    "api/models.py": None,
    "capabilities/sites/cli.py": None,
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
    from blitzecdn.capabilities.sites.domain import CdnSite
    from blitzecdn.core.ansible.mapping import site_to_ansible
    from blitzecdn.core.validation import OmittedWhenEmpty

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


# ----------------------------------------------------------------------
# Ansible ownership
#
# The other half of a vertical slice, and the half that used to be missing.
# A capability whose Python detaches cleanly while its roles, its templates and
# its fleet settings stay behind in the control plane's `ansible/` tree is not
# detachable at all: uninstalling the wheel leaves an edge still being told to
# provision a GeoIP database by a role core carries.
#
# Three rules hold it, and each one refuses a different way of half-doing the
# move: no capability's name in core's Ansible, no capability's role in core's
# tree, and a contributed role that the edge play really runs.
# ----------------------------------------------------------------------

#: The repository-level Ansible tree, which is the platform's and nobody
#: else's: the base host, the container engine, the firewall, the edge runtime
#: contract, `blitzecdn_nginx`, and the slot the installed capabilities fill.
CORE_ANSIBLE = REPO_ROOT / "src/blitzecdn/ansible"

#: Words that name an optional capability's *implementation* rather than a
#: setting core legitimately renders. `geoip2` is the Nginx module directive,
#: `geoipupdate` the updater container, `maxmind` the account they authenticate
#: to, `njs`/`js_import` the module system the challenge is written in — none
#: of them can appear in a tree that must converge an edge identically whether
#: or not any distribution is installed.
#:
#: `resolved.conf.d` is the resolver drop-in's directory: the file there is
#: written by `blitzecdn-resolver`'s role and removed by that same package's
#: teardown role, so core neither creates nor deletes it. It used to be listed
#: in `blitzecdn_teardown`'s defaults, which meant a role installed on every
#: controller carried the path of a capability that may not be installed.
#: `sshd_config.d` and `fail2ban` are the same case one package later: both
#: files are `blitzecdn-hardening`'s, and `blitzecdn_hardening_teardown` is
#: what removes them.
#:
#: `$blitzecdn_country` and `under_attack_mode` are deliberately *not* here.
#: They are site settings core renders from desired state, and a site that asks
#: for either is refused by name before a play starts; that split — core reads
#: the variable, the capability defines it — is the whole design and is
#: asserted from the packages' own tests, which can see both sides.
CAPABILITY_IMPLEMENTATION_WORDS = (
    "resolved.conf.d",
    "maxmind",
    "geolite",
    "geoip2",
    "geoipupdate",
    "geoip_enabled",
    "js_import",
    "under_attack_secret",
    "under_attack_enabled",
)

#: There are no exemptions, and there is no longer a mechanism for one.
#:
#: There used to be exactly one: the task that started the managed edge image
#: and asked whether it could load the modules it was built with named GeoIP2,
#: Brotli and njs, on the grounds that the *image* carries them whether or not
#: any distribution is installed. That grounding is what changed. The modules
#: an edge loads are declared by the capabilities that need them, resolved by
#: `blitzecdn.core.plugins.resolution.resolve_edge_modules` and rendered into
#: the edge's own `load_module` list — so the probe now asserts against that
#: resolved list and has no module name of its own to hold. Nothing in the tree
#: is exempt.
CAPABILITY_WORD_EXEMPTIONS: frozenset[str] = frozenset()


def _core_ansible_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_ANSIBLE.rglob("*")
        if path.is_file()
        and path.suffix in {".yml", ".yaml", ".j2", ".cfg", ".sh"}
        and "__pycache__" not in path.parts
    )


def test_core_ansible_names_no_capabilitys_implementation():
    """The platform tree provisions the platform, and stops there.

    A grep, deliberately, because the failure it catches is textual: a task
    that fetches a MaxMind database or a template that emits `js_import` is
    implementation belonging to a wheel, wherever it is written and whatever
    the variable around it is called.

    A comment is exempt. Several of the files here explain *why* something is
    no longer present, and forbidding the explanation would mean removing the
    only record of the decision.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number} names {word!r}"
        for path in _core_ansible_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if not line.lstrip().startswith(("#", ";"))
        and str(path.relative_to(CORE_ANSIBLE)) not in CAPABILITY_WORD_EXEMPTIONS
        for word in CAPABILITY_IMPLEMENTATION_WORDS
        if word in line.lower()
    ]
    assert offenders == [], (
        "core's Ansible tree carries an optional capability's implementation; "
        "it belongs in that distribution's own roles/ directory: " + str(offenders)
    )


def test_no_capability_owned_role_survives_in_the_core_tree():
    """The same rule as a directory listing, which is what a duplicate is.

    A role left behind in `ansible/roles/` after its package shipped one would
    not merely be dead: `resolve_role_search_path` puts core's directory first,
    so the stale copy would *win* and every edge would converge the version
    nobody is maintaining.
    """
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
        if directory.is_dir()
    }
    core = {
        directory.name
        for directory in (CORE_ANSIBLE / "roles").iterdir()
        if directory.is_dir()
    }

    assert contributed, "no optional distribution ships an Ansible role at all"
    assert contributed & core == set(), (
        "a role is shipped by both core and a package; core's directory is "
        f"searched first, so the package's copy would never run: {contributed & core}"
    )


def test_core_nginx_templates_are_capability_neutral():
    """Optional directive implementations must live in contributed resources."""
    templates = CORE_ANSIBLE / "roles/blitzecdn_nginx/templates"
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(templates.rglob("*.j2"))
    ).lower()
    forbidden = (
        "brotli ",
        "gzip_comp_level",
        "listen 443 quic",
        "alt-svc",
        "proxy_cache ",
        "proxy_cache_key",
        "blitzecdn_under_attack",
        "js_content",
        "$blitzecdn_country",
        "geoip2 ",
    )

    assert [directive for directive in forbidden if directive in text] == []


def test_the_decommission_play_names_no_capability_and_fills_its_slot():
    """The other play a capability contributes to, and the harder direction.

    Converging is forgiving: a capability whose role did not run leaves an edge
    unconverged, and the next deploy fixes it. Decommissioning is not. The play
    runs while the host is still in inventory and there is no way back to it
    afterwards, so a capability's files either come off here or stay on that
    host forever.

    The slot must therefore be *in* the play, must name no capability, and must
    come before `blitzecdn_teardown` — that role ends by asserting the host is
    clean and failing the run if anything survived, which is the verdict on the
    whole decommission and cannot be passed before half the removal has
    happened.
    """
    play = (CORE_ANSIBLE / "playbooks/decommission.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, (
            f"the decommission play names {role}, which ships in a wheel"
        )
    assert "blitzecdn_teardown_capability_roles" in play
    assert play.index("blitzecdn_teardown_capability_roles") < play.index(
        "role: blitzecdn_teardown"
    )


def test_core_s_teardown_removes_no_path_that_belongs_to_a_wheel():
    """A role installed on every controller may not know a wheel's paths.

    `blitzecdn_teardown` is core's, so it runs on every decommission whatever
    is installed. Every path in it is therefore a claim core can still make
    when a capability has been detached — its own trees, the shared runtime
    directories, and units matched by prefix rather than listed. A capability's
    own file is removed by that capability's teardown role, in the slot before
    this one.

    The whole role rather than only its defaults, because the leak this refuses
    has appeared in all three files: the SSH policy and the Fail2Ban jail were
    paths in `defaults`, a task removing them, *and* two handlers reloading
    services only `blitzecdn-hardening` installs. A defaults-only check would
    have passed while core still restarted `fail2ban` on every decommission.
    """
    role = CORE_ANSIBLE / "roles/blitzecdn_teardown"
    contributed_paths = ("resolved.conf.d", "sshd_config.d", "fail2ban", "sshd")

    for source in sorted(role.rglob("*.yml")):
        for line in source.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for path in contributed_paths:
                assert path not in line, (
                    f"{source.relative_to(CORE_ANSIBLE)} names {path}, which "
                    "belongs to a wheel core cannot depend on being installed"
                )


def test_the_edge_play_names_no_capability_and_fills_both_slots():
    """The play is the platform's, and it is what makes a contribution run.

    Two properties in one place because they are the same decision. The play
    must name no capability's role — that is what "no edit to add a package"
    means — and it must include both lists the control plane composes, or a
    contributed role would resolve by name and never execute.

    Both slots, because a package declaring `host_roles` and getting silence is
    the failure this catches: the edge slot would still run, the play would
    still report success, and the SSH policy nobody noticed was missing is
    exactly the kind of absence that surfaces months later.
    """
    play = (CORE_ANSIBLE / "playbooks/edge.yml").read_text(encoding="utf-8")
    contributed = {
        directory.name
        for package in optional_packages()
        for directory in (package / "src").rglob("ansible/roles/*")
        if directory.is_dir()
    }

    for role in contributed:
        assert role not in play, f"the edge play names {role}, which ships in a wheel"
    assert "blitzecdn_capability_roles" in play
    assert "blitzecdn_host_capability_roles" in play

    slot = (CORE_ANSIBLE / "roles/blitzecdn_capabilities/tasks/main.yml").read_text(
        encoding="utf-8"
    )
    assert "blitzecdn_capabilities_roles" in slot
    assert "include_role" in slot
    # One role, two invocations. Ansible runs a role again when its parameters
    # differ, but only `allow_duplicates` states that on purpose — without it,
    # an edit that made both slots read the same variable would silently
    # collapse them into one and converge the host slot's roles nowhere.
    duplicates = (
        CORE_ANSIBLE / "roles/blitzecdn_capabilities/meta/main.yml"
    ).read_text(encoding="utf-8")
    assert "allow_duplicates: true" in duplicates


def test_every_contributed_edge_role_is_shipped_by_the_plugin_that_asks_for_it():
    """A contribution that names a role its own wheel lacks is refused early.

    Ansible would refuse it too, but only after the engine is installed, the
    image pulled and the play half-way through an edge — and its message names
    the role, never the distribution that asked for it.
    """
    for package in optional_packages():
        for roles in (package / "src").rglob("ansible/roles"):
            available = {path.name for path in roles.iterdir() if path.is_dir()}
            plugin = (roles.parent.parent / "plugin.py").read_text(encoding="utf-8")
            for role in available:
                if role in plugin:
                    assert (roles / role / "tasks/main.yml").is_file(), role


def test_optional_roles_do_not_depend_on_another_packages_role_order():
    """Alphabetical contribution order is deterministic, never dependency resolution."""
    package_roles = {
        package: {
            role.name
            for roles in (package / "src").rglob("ansible/roles")
            for role in roles.iterdir()
            if role.is_dir()
        }
        for package in optional_packages()
    }
    offenders = []
    for package, own_roles in package_roles.items():
        foreign_roles = set().union(
            *(roles for owner, roles in package_roles.items() if owner != package)
        )
        for task_file in (package / "src").rglob("ansible/roles/*/tasks/*.yml"):
            text = task_file.read_text(encoding="utf-8")
            offenders.extend(
                f"{task_file.relative_to(REPO_ROOT)} names {role}"
                for role in sorted(foreign_roles - own_roles)
                if f"role: {role}" in text or f"name: {role}" in text
            )

    assert offenders == []


def test_core_carries_no_setting_named_for_an_optional_capability():
    """`Settings` is the platform's configuration, not a union of everyone's.

    A field named for a capability is loaded by every installation, including
    the ones that will never have the distribution — and a capability this
    repository has never heard of could not be configured at all. The generic
    answer is `capability_environment`, whose keys installed plugins must claim
    explicitly before core forwards them.
    """
    from blitzecdn.core.config import Settings

    # Implementation names, not capability tokens. A field called `backup_dir`
    # names a directory the platform creates and protects — `install.sh update`
    # writes there before it changes anything, with or without the `backup`
    # distribution — while a field called `maxmind_license_key` can only be
    # read by one wheel and is meaningless without it. The second kind is what
    # this refuses.
    forbidden = ("maxmind", "geolite", "under_attack", "brotli", "geoip", "njs")
    offenders = [
        name for name in Settings.model_fields for token in forbidden if token in name
    ]
    assert offenders == [], offenders


# --- the image build inputs ship inside the package, like the Ansible --------


def test_the_published_docker_paths_are_the_ones_in_the_checkout():
    """`blitzecdn.docker` and the checkout tree are the same directory.

    The module resolves through `importlib.resources`, which in a checkout
    with an editable install answers the checkout — so every other suite can
    read the constants and still be reading the files under review. The day
    that stops being true is the day the packaging moved and nothing said so.
    """
    from blitzecdn import docker

    assert docker.EDGE_CONTEXT == CORE_DOCKER / "edge"
    assert docker.CONTROL_PLANE_DOCKERFILE == CORE_DOCKER / "control-plane/Dockerfile"
    for constant in (
        docker.EDGE_DOCKERFILE,
        docker.EDGE_MODULE_PROBE_CONF,
        docker.CONTROL_PLANE_DOCKERFILE,
        docker.CONTROL_PLANE_DOCKERIGNORE,
    ):
        assert constant.is_file(), constant


def test_no_tracked_file_names_the_old_top_level_docker_directory():
    """The build inputs moved into the wheel; nothing may point back.

    A textual guard, because that is the shape of the regression: a new script
    or workflow copying the old `docker/edge` incantation would work in a
    checkout and fail on an installed controller, which is the exact failure
    mode the move removed. Python callers should read the constants; the
    justfile, the two root-run shell scripts and the release workflow cannot
    import anything, so they spell the path under `src/blitzecdn/` and this
    only refuses the pre-move form.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git, "this asks git which files are tracked"
    tracked = subprocess.run(  # noqa: S603 - fixed argv built here
        [git, "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    offenders = [
        f"{name}:{number}"
        for name in tracked
        for path in ((REPO_ROOT / name),)
        # This module is the one file that has to spell the pre-move form:
        # the needles below are it. Prose elsewhere is deliberately *not*
        # exempt — a comment telling somebody to build `docker/edge` sends
        # them somewhere that no longer exists just as surely as a command
        # would.
        if path.is_file()
        and not name.startswith("src/blitzecdn/docker/")
        and path != Path(__file__).resolve()
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        )
        for stale in ("docker/edge", "docker/control-plane")
        if stale in line and f"src/blitzecdn/{stale}" not in line
    ]
    assert offenders == [], (
        "these name the pre-move `docker/` directory, which only a checkout "
        f"has: {offenders}"
    )
