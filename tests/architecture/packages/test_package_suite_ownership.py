"""Package test ownership boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from package_boundary_support import (
    _BEHAVIOUR_LAYERS,
    _built_in_capabilities,
    _imports,
    _optional_import_roots,
    _packages,
)
from paths import REPO_ROOT, SOURCE

# --- tests travel with their package ----------------------------------------


@pytest.mark.parametrize("package", _packages(), ids=lambda path: path.name)
def test_an_optional_packages_tests_live_inside_it(package: Path):
    assert list((package / "tests").glob("test_*.py"))


def test_a_shipped_file_names_no_test_that_has_moved():
    """A pointer that ships to an operator has to point at something.

    Role defaults and source docstrings cite the test that holds the invariant
    they describe — `blitzecdn_cache`'s defaults say a purge deletes nothing if
    its path disagrees with what `blitzecdn_nginx` converged, and names the
    suite that asserts they agree. That is the most useful comment in the file
    and the reason to keep writing them.

    Seven of them named a path that no longer existed. None was wrong when it
    was written: the suite grew per-capability and per-layer directories, every
    file moved, and prose naming them does not move with `git mv`. At 353
    renames in fifty commits that is not an oversight anybody could have caught
    by reading.

    Shipped files only — `src/` and `packages/*/src/`. A role's `defaults` and
    `tasks` are installed on an operator's machine, so a dead path there is one
    they cannot resolve at all.
    """
    shipped = [
        path
        for root in (SOURCE, *(package / "src" for package in _packages()))
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yml", ".yaml", ".j2"}
        and "__pycache__" not in path.parts
    ]
    cited = re.compile(r"tests/[\w/]*test_\w+\.py")
    offenders = sorted(
        {
            f"{path.relative_to(REPO_ROOT)} names {named}"
            for path in shipped
            for named in cited.findall(path.read_text(encoding="utf-8"))
            if not (REPO_ROOT / named).is_file()
        }
    )
    assert offenders == []


@pytest.mark.parametrize(
    "capability", _built_in_capabilities(), ids=lambda path: path.name
)
def test_a_built_in_capabilitys_tests_live_in_its_own_directory(capability: Path):
    """The built-in half of the rule above, which only wheels were held to.

    A wheel keeps its tests inside it and the rule before this one says so, so
    `packages/blitzecdn-cache` is removable in one move. The built-ins had no
    counterpart, and three capabilities had drifted out of `tests/capabilities`
    entirely: `MaintenanceService` was asserted in `tests/platform/test_queue.py`
    — a file about Dramatiq, which the service does not touch — while
    `WorkflowCoordinator` and `check_resolver` had no direct test at all, their
    behaviour covered incidentally by suites that would still pass if the rules
    they own changed.

    Directory, not file count: what this refuses is a capability whose tests
    have no home, not a capability with few of them.
    """
    if not any((capability / layer).is_dir() for layer in _BEHAVIOUR_LAYERS):
        pytest.skip(f"{capability.name} is a contract, tested where it composes")
    home = REPO_ROOT / "tests" / "capabilities" / capability.name
    assert list(home.glob("test_*.py")), (
        f"{capability.name} decides something and has nowhere of its own to "
        f"assert it; add tests/capabilities/{capability.name}/"
    )


def test_the_control_plane_suite_names_no_optional_package():
    """Core's tests do not import a capability that may not be installed.

    The exceptions are this package suite and its lifecycle companion, which are *about*
    the packages and would be meaningless without naming them. Everything else
    under `tests/` must pass with every optional distribution uninstalled,
    because that is the configuration `just test-core-only` runs.
    """
    optional = _optional_import_roots()
    here = {"test_lifecycle.py"}
    offenders = [
        f"{path.name} imports {imported}"
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path.name not in here and not path.is_relative_to(Path(__file__).parent)
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
        "test_lifecycle.py",
        # `skip_tests_a_detached_capability_cannot_answer` lives here.
        "control_plane_fixtures.py",
    }
    offenders = [
        f"{path.name} names {named}"
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path.name not in allowed and not path.is_relative_to(Path(__file__).parent)
        for named in sorted(_dynamic_package_names(path))
        if named in optional
    ]
    assert offenders == []
