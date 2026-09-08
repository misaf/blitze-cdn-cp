"""Package imports boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from package_boundary_support import (
    _import_package,
    _imports,
    _manifest,
    _next_major_bound,
    _optional_import_roots,
    _packages,
    _source_files,
)
from paths import SOURCE
from published_surface import (
    _FORBIDDEN_SDK_MODULES,
    _PUBLIC_CAPABILITY_MODULES,
    _PUBLIC_SDK_PREFIXES,
    facade_private_modules,
)

from blitzecdn.composition import BUILTIN_PLUGINS, load_control_plane_plugins
from blitzecdn.core.plugins import (
    build_plugin_manager,
    register_builtins,
)

# --- core does not know the implementations ---------------------------------


def test_core_never_imports_an_optional_package():
    """The load-bearing rule, checked by name over the whole distribution.

    Not scoped to `composition/`, because the composition root is only the
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
    assert all(
        metadata.required for metadata in register_builtins(manager, BUILTIN_PLUGINS)
    )

    installed = load_control_plane_plugins()
    builtins = load_control_plane_plugins(entry_point_group=None)
    builtin_names = {metadata.name for metadata in builtins.plugins}
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
    bound = _next_major_bound()
    for requirement in project["dependencies"]:
        assert bound in requirement, (
            f"{requirement} does not carry {bound}. Every workspace dependency "
            "is capped at the next major, so a wheel cannot load against a "
            "control plane whose ABI it was not written for."
        )


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


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_package_imports_only_public_contracts(package: Path):
    """What a package may name inside the control plane, and what it may not.

    An allowlist rather than a denylist, because the failure this guards is a
    package reaching for something core never meant to publish and nobody
    noticing until core moves it. The list is the public SDK: adding to it is a
    deliberate decision about what BlitzeCDN promises an installed capability.

    `TYPE_CHECKING` imports count. `ControlPlane` is annotated by every package
    that receives one, and that is knowledge of `blitzecdn.composition` whichever
    block it sits in — so it is written as a guarded, annotation-only import
    that this test allows by name, rather than excused by a rule that would
    also let a runtime import through.

    A prefix admits every module beneath it, which over-admits wherever the
    package is a façade: `blitzecdn.core.plugins.types` was as importable as
    `blitzecdn.core.plugins`, and a wheel that took the deep path would break
    on a refactor that never touched the name it imported.
    `facade_private_modules` derives those — every public name reachable from
    the package above — and they are refused here, so the only path into the
    plugin SDK is the one the golden file pins.
    """
    allowed = (*_PUBLIC_SDK_PREFIXES, *_PUBLIC_CAPABILITY_MODULES)
    behind_a_facade = facade_private_modules()
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
            # The symbol too, not only the module: `from ...plugins.types import
            # CliCommandGroup` is judged as `...plugins.types.CliCommandGroup`
            # further down, and a rule that refused only the module would let
            # every name inside it through the door beside it.
            if any(
                name == module or name.startswith(f"{module}.")
                for module in behind_a_facade
            ):
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
