"""The public surfaces, held to a file that changes only on purpose.

The routes an API client calls, the commands an operator's script types, the
variables in an operator's inventory, and the database columns. A change to a
public name arrives as a diff somebody has to approve.

**When one of these fails, read the diff before regenerating it.** The
question is whether this may change given who depends on it. `just refreeze`
records a yes; it is not a way to make a test go away.
"""

from __future__ import annotations

import difflib

import pytest
import support.frozen_surfaces as surfaces
from support.frozen_surfaces import FROZEN


def _compare(name: str, current: str) -> None:
    path = FROZEN / f"{name}.txt"
    if not path.is_file():
        pytest.fail(
            f"{path} does not exist. Run `just refreeze` to create it, then "
            "read what it contains before committing: this file is a promise."
        )
    expected = path.read_text(encoding="utf-8")
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


def test_the_ansible_interface_is_what_it_was():
    _compare("ansible", surfaces.ansible_surface())


def test_the_database_schema_is_what_it_was():
    _compare("schema", surfaces.schema_surface())


def test_every_surface_found_something_to_freeze(settings):
    """The failure mode a golden file cannot catch on its own.

    Each generator reaches into framework internals (Typer's click tree, FastAPI's
    router wrappers, SQLModel's metadata). One that stopped finding anything would
    match an empty golden, so each surface has a loose floor.
    """
    floors = {
        "cli": (surfaces.cli_surface(), 40),
        "http": (surfaces.http_surface(settings), 150),
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

    `blitzecdn --help` must not create a database, and `cli_surface` relies on the
    same thing.
    """
    from blitzecdn.entrypoints.cli import main

    assert main.app.registered_groups
    assert "domain" in {group.name for group in main.app.registered_groups}
