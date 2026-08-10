"""The layering rule, enforced instead of merely documented.

``domain`` knows nothing but itself. ``ports`` declares the interfaces over the
outside world and may see the domain. ``application`` orchestrates the domain
through those ports and must never reach for a concrete adapter. Only the
composition root and the two entry points are allowed to know both halves.

This used to be a convention kept by review. Reviews miss a single import, and
the failure is invisible until someone tries to test a service without a
database — so it is checked here, over the real source tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parent.parent / "src" / "blitzecdn"

#: Packages that carry an I/O concern. A domain or application module importing
#: one of these has reached past its layer.
_ADAPTERS = (
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
)


def _modules(package: str) -> list[Path]:
    root = _SOURCE / package
    if root.is_dir():
        return sorted(root.rglob("*.py"))
    return [_SOURCE / f"{package}.py"]


def _imports(path: Path) -> set[str]:
    """Every module this file imports, as dotted names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _violations(package: str, forbidden: tuple[str, ...]) -> list[str]:
    def banned(imported: str) -> bool:
        root = imported.split(".")[0]
        return any(
            root == name or imported.startswith(f"{name}.") for name in forbidden
        )

    return [
        f"{path.name} imports {imported}"
        for path in _modules(package)
        for imported in sorted(_imports(path))
        if banned(imported)
    ]


def test_domain_imports_nothing_but_itself():
    """The domain is provider-independent: no I/O, no adapters, no framework."""
    assert (
        _violations(
            "domain",
            (
                *_ADAPTERS,
                "blitzecdn.infrastructure",
                "blitzecdn.application",
                "blitzecdn.control_plane",
                "blitzecdn.api",
                "blitzecdn.cli",
                "blitzecdn.config",
                "blitzecdn.ports",
            ),
        )
        == []
    )


def test_ports_only_face_the_domain():
    """Ports describe the outside world; they must not import an implementation."""
    assert (
        _violations(
            "ports",
            (
                *_ADAPTERS,
                "blitzecdn.infrastructure",
                "blitzecdn.application",
                "blitzecdn.control_plane",
            ),
        )
        == []
    )


def test_application_never_touches_a_concrete_adapter():
    """Services depend on ports. A concrete adapter here re-couples the layers."""
    assert (
        _violations(
            "application",
            (
                *_ADAPTERS,
                "blitzecdn.infrastructure",
                "blitzecdn.control_plane",
                "blitzecdn.api",
                "blitzecdn.cli",
            ),
        )
        == []
    )


def test_infrastructure_does_not_depend_on_the_application():
    """Adapters are used by the application, never the other way round."""
    assert (
        _violations(
            "infrastructure",
            ("blitzecdn.application", "blitzecdn.control_plane", "blitzecdn.api"),
        )
        == []
    )


@pytest.mark.parametrize("module", ["control_plane", "cli", "acme_hook"], ids=str)
def test_the_wiring_modules_do_wire(module):
    """A sanity check on the rule above.

    If one of these stops importing infrastructure the layering tests would
    start passing for the wrong reason — nothing left to couple — so the list
    of modules that legitimately know both halves is kept honest here.
    """
    imports: set[str] = set()
    for path in _modules(module):
        imports |= _imports(path)
    assert any(name.startswith("blitzecdn.infrastructure") for name in imports)


def test_the_api_reaches_infrastructure_only_through_the_control_plane():
    """The HTTP layer builds nothing itself.

    ``create_app`` used to construct a ``ControlPlane``, which meant the API
    module transitively chose a database and a runner. It now asks the
    composition root, so the whole adapter choice lives in one place.
    """
    assert _violations("api", ("blitzecdn.infrastructure",)) == []
