"""Every capability and provider is built from the same parts, under one name each.

A part may be a module or a package — `cli.py` and `dns/cli/` are both the
CLI — but it may not be a third name for a job one of these already does. A
new module goes into the part it belongs to, and the layering rules that part
is held to come with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from paths import SOURCE

_CAPABILITIES = SOURCE / "capabilities"
_PROVIDERS = SOURCE / "providers"

PARTS = frozenset(
    {
        "__init__",
        "feature",  # the declaration: routes, commands, roles, fragments
        "contract",  # pure values another capability may import
        "settings",  # this capability's own configuration
        "domain",
        "service",
        "ports",
        "adapters",
        "api",
        "cli",
        "wiring",  # how the slice is built, and what it renders and runs
        "ansible",  # roles and plays shipped to edges
        "nginx",  # fragments rendered into each release
    }
)

#: Every entry outside `PARTS` today, and what it is. Each one is a decision
#: still to make, not a second convention: the list only shrinks, and an entry
#: whose path has gone fails until it is removed from here too.
KNOWN_DEVIATIONS = {
    "capabilities/origins/reporting.py": (
        "parses the report an edge publishes, which is adapter work; "
        "`automatic_ssl` reads it too, so it is published across capabilities"
    ),
    "capabilities/tls/acme_hook.py": (
        "a process certbot starts, so an entry point rather than a part"
    ),
}

#: Slices nested inside a capability rather than beside it. `test_layering`
#: refuses them a top-level package of their own (`certificates` and
#: `automatic_ssl` are strategies `tls` owns), so they stay where they are and
#: are held to the same skeleton one level down.
NESTED_SLICES = frozenset(
    {
        "capabilities/tls/automatic_ssl",
        "capabilities/tls/certificates",
    }
)


def _has_content(path: Path) -> bool:
    """A file, or a directory holding anything other than bytecode caches."""
    if path.is_file():
        return True
    return any(
        child.is_file() and "__pycache__" not in child.parts
        for child in path.rglob("*")
    )


def _slices() -> list[Path]:
    roots = [
        path
        for parent in (_CAPABILITIES, _PROVIDERS)
        for path in sorted(parent.iterdir())
        if path.is_dir() and (path / "__init__.py").is_file()
    ]
    return roots + [SOURCE / name for name in sorted(NESTED_SLICES)]


def _children(slice_: Path) -> list[Path]:
    return [
        child
        for child in sorted(slice_.iterdir())
        if child.name != "__pycache__" and _has_content(child)
    ]


@pytest.mark.parametrize(
    "slice_", _slices(), ids=lambda path: str(path.relative_to(SOURCE))
)
def test_a_slice_holds_only_the_named_parts(slice_: Path) -> None:
    offenders = [
        name
        for child in _children(slice_)
        for name in (str(child.relative_to(SOURCE)),)
        if child.name.removesuffix(".py") not in PARTS
        and name not in KNOWN_DEVIATIONS
        and name not in NESTED_SLICES
    ]
    assert offenders == [], (
        f"{offenders} are not parts of a slice; put each in one of "
        f"{sorted(PARTS)}, or record why it is not in KNOWN_DEVIATIONS"
    )


def test_every_recorded_exception_still_exists() -> None:
    stale = sorted(
        name
        for name in (*KNOWN_DEVIATIONS, *NESTED_SLICES)
        if not (SOURCE / name).exists()
    )
    assert stale == [], f"{stale} no longer exist; remove them from this module"
