"""The public surfaces, held to a file that changes only on purpose.

`tests/architecture/` is 5,600 lines that guard where a module sits. Every one
of those failures is fixable with `git mv`. These six guard the things that are
not: the routes an API client calls, the commands an operator's script types,
the hook signatures a third-party wheel implements, the names in a published
module, the variables in an operator's own inventory, and the columns in a
database that will outlive every one of them.

Nothing held any of that before. A commit could rename an Ansible variable or
change a hookspec and CI stayed green, because the tests exercising it were
edited in the same commit. That is right for a project with no installations —
`3.0.0`, one migration, nothing published — and it stops being right the day
somebody installs. The point of freezing now is that the surfaces are still
free to change: a golden file is how a deliberate change stays distinguishable
from an accidental one.

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

import frozen_surfaces as surfaces
import pytest
from frozen_surfaces import FROZEN, installed
from published_surface import _PUBLIC_SDK_PREFIXES


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
    assert "site" in {group.name for group in main.app.registered_groups}
