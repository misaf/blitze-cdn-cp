"""Every capability is built from the same parts, under one name each.

A part may be a module or a package — `cli.py` and `dns/cli/` are both the
CLI — but it may not be a third name for a job one of these already does. A
new module goes into the part it belongs to, and the layering rules that part
is held to come with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.paths import SOURCE

_CAPABILITIES = SOURCE / "capabilities"

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

#: Slices nested inside a capability rather than beside it, held to the same
#: skeleton one level down.
NESTED_SLICES = frozenset(
    {
        "capabilities/http/http3",
        "capabilities/security/geoip",
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
        for path in sorted(_CAPABILITIES.iterdir())
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
        if child.name.removesuffix(".py") not in PARTS and name not in NESTED_SLICES
    ]
    assert offenders == [], (
        f"{offenders} are not parts of a slice; put each in one of {sorted(PARTS)}"
    )


def test_every_nested_slice_still_exists() -> None:
    stale = sorted(name for name in NESTED_SLICES if not (SOURCE / name).exists())
    assert stale == [], f"{stale} no longer exist; remove them from this module"


def test_shared_site_values_are_not_a_capability() -> None:
    assert list((_CAPABILITIES / "serving").rglob("*.py")) == []
