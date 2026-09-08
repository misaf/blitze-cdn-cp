"""The public surfaces, held to a file that changes only on purpose.

`tests/architecture/` is 5,600 lines that guard where a module sits. Every one
of those failures is fixable with `git mv`. These six guard the things that are
not: the routes an API client calls, the commands an operator's script types,
the hook signatures a third-party wheel implements, the names in a published
module, the variables in an operator's own inventory, and the columns in a
database that will outlive every one of them.

Nothing held any of that before. A commit could rename an Ansible variable or
change a hookspec and CI stayed green, because the tests exercising it were
edited in the same commit — a deliberate change and an accidental one looked
identical, and neither was visible in review. These files are the difference:
a change to a public name now arrives as a diff somebody has to approve.

`4.0.0` is the first release they exist for, and the major is not incidental.
A host follows tags inside its major line on every update, so a renamed
Ansible variable shipped as a minor would land on a running fleet unasked.
See COMPATIBILITY.md for what each surface promises and what changing it
costs.

**When one of these fails, read the diff before regenerating it.** The question
it asks is not "is the new surface correct" but "may this change, given who is
already depending on the old one". `just refreeze` exists for when the answer is
yes; it is not a way to make a test go away.

Two configurations, one golden file. `just test` runs with every optional
distribution installed and `just test-core-only` with none, so each line names
the distribution that promises it and the comparison is filtered to whatever is
installed. `frozen_surfaces.installed` asks the import system rather than
trusting the current surface — filtering by what was *found* would let a whole
wheel's routes vanish without a failure, which is the deletion this is most
meant to catch.
"""

from __future__ import annotations

import difflib
import re

import frozen_surfaces as surfaces
import pytest
from frozen_surfaces import FROZEN, installed
from paths import REPO_ROOT
from published_surface import _PUBLIC_SDK_PREFIXES, facade_private_modules


def _for_this_environment(golden: str) -> str:
    """The golden, minus the distributions this environment does not have."""
    return "".join(
        f"{line}\n" for line in golden.splitlines() if installed(line.split("\t", 1)[0])
    )


def _compare(name: str, current: str) -> None:
    path = FROZEN / f"{name}.txt"
    if not path.is_file():
        pytest.fail(
            f"{path} does not exist. Run `just refreeze` to create it, then "
            "read what it contains before committing: this file is a promise."
        )
    expected = _for_this_environment(path.read_text(encoding="utf-8"))
    if expected == current:
        return
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            fromfile=f"frozen/{name}.txt",
            tofile="this tree",
            lineterm="",
        )
    )
    pytest.fail(
        f"the published {name} surface changed:\n\n{diff}\n\n"
        "Every line here is something outside this repository can depend on. "
        "If the change is intended, `just refreeze` records it — and if this "
        "surface is already released, it needs a version decision first."
    )


def test_the_command_line_is_what_it_was():
    _compare("cli", surfaces.cli_surface())


def test_the_http_api_is_what_it_was(settings):
    _compare("http", surfaces.http_surface(settings))


def test_the_plugin_abi_is_what_it_was():
    _compare("plugin_abi", surfaces.plugin_abi_surface())


def test_the_published_sdk_is_what_it_was():
    _compare("sdk", surfaces.sdk_surface(_PUBLIC_SDK_PREFIXES))


def test_the_ansible_interface_is_what_it_was():
    _compare("ansible", surfaces.ansible_surface())


def test_the_database_schema_is_what_it_was():
    _compare("schema", surfaces.schema_surface())


def test_every_surface_found_something_to_freeze(settings):
    """The failure mode a golden file cannot catch on its own.

    Each generator reaches into a framework's internals — Typer's click tree,
    FastAPI's router wrappers, SQLModel's metadata — and every one of those has
    changed shape before. `_api_routes` already had to be rewritten twice
    against one FastAPI version. A generator that quietly stopped finding
    anything would leave an empty surface matching an empty golden, and the pin
    would read green while guarding nothing.

    So each surface is held to a floor. The numbers are deliberately loose: this
    asks whether the generator still works, not what the contract contains —
    that is what the golden files are for.
    """
    floors = {
        "cli": (surfaces.cli_surface(), 40),
        "http": (surfaces.http_surface(settings), 150),
        "plugin_abi": (surfaces.plugin_abi_surface(), 50),
        "sdk": (surfaces.sdk_surface(_PUBLIC_SDK_PREFIXES), 100),
        "ansible": (surfaces.ansible_surface(), 100),
        "schema": (surfaces.schema_surface(), 40),
    }
    thin = {
        name: len(text.splitlines())
        for name, (text, floor) in floors.items()
        if len(text.splitlines()) < floor
    }
    assert thin == {}, (
        f"a surface generator has stopped seeing most of its surface: {thin}. "
        "The framework it reads probably changed shape; fix the generator "
        "rather than the floor."
    )


def test_the_command_line_needs_no_database_to_describe_itself():
    """The property that makes freezing the CLI cheap, asserted where it is used.

    `blitzecdn --help` must not create and migrate a database, and
    `cli_surface` depends on the same thing: it imports the command tree and
    reads it. If building the tree ever started constructing a control plane,
    this suite would start writing SQLite files as a side effect of describing
    itself — and the operator-facing property would already be broken.
    """
    from blitzecdn.cli import main

    assert main.app.registered_groups
    assert "domain" in {group.name for group in main.app.registered_groups}


def test_what_the_sdk_publishes_is_what_a_wheel_is_allowed_to_import():
    """The two halves of the narrowing, checked against each other.

    A published name is pinned at its shallowest path here, and a wheel is
    refused the deeper one by `test_an_optional_package_imports_only_public_
    contracts`. Those are two derivations of one idea — that a module behind a
    façade is an implementation detail — written in different files against
    different data, and either could drift into disagreeing with the other.

    Disagreement in one direction leaves a module a wheel may import and
    nothing pins, which is the freeze the golden was supposed to provide. In
    the other it leaves a name pinned at a path no wheel may use, which is a
    promise nobody can accept. So neither is allowed: a module the import rule
    calls private contributes no line to this surface.
    """
    private = facade_private_modules()
    published = {
        line.split("\t")[2].rsplit(".", 1)[0]
        for line in surfaces.sdk_surface(_PUBLIC_SDK_PREFIXES).splitlines()
        if line
    }
    assert published & private == set()


def test_the_compatibility_policy_cites_things_that_exist():
    """The document that says what is promised, held to the same standard.

    COMPATIBILITY.md is a policy about names, written in names: the six golden
    files, the tests that enforce them, and the helpers that derive what is
    public. Every one of those can be renamed by a commit that never opens the
    document, and a policy citing a test that no longer exists is worse than no
    policy — it reads as enforcement and is decoration.

    This is the same failure the built-in capability map had, found the same
    way: prose naming code, with nothing checking the names. So the citations
    are extracted and resolved. What is *said* about each is still a human's
    job; that it refers to something real is not.
    """
    document = (REPO_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")

    goldens = set(re.findall(r"`(\w+)\.txt`", document))
    assert goldens, "the surface table names no golden files; the format moved"
    missing_goldens = {
        name for name in goldens if not (FROZEN / f"{name}.txt").is_file()
    }
    assert missing_goldens == set(), f"COMPATIBILITY.md names {missing_goldens}"

    cited = set(re.findall(r"`(test_\w+)`", document))
    assert cited, "the enforcement table cites no tests; the format moved"
    defined = {
        name
        for path in (
            *REPO_ROOT.glob("tests/**/*.py"),
            *REPO_ROOT.glob("packages/*/tests/*.py"),
        )
        for name in re.findall(
            r"^def (test_\w+)", path.read_text(encoding="utf-8"), re.M
        )
    }
    assert cited <= defined, (
        f"COMPATIBILITY.md cites tests that do not exist: {sorted(cited - defined)}. "
        "Rename it there too, or say what replaced it."
    )
