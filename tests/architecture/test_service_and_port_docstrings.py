"""The surfaces other code depends on say what they promise.

A service method is an operation callers build on: what it guarantees, what it
raises, and whether the caller must already hold the deployment lock belong in
its docstring. A port method is the contract an adapter must honour, and the
fake in a test suite is written from the same words. Both are required to have
a docstring.

Nothing else is. A value object's fields, a property, an API route whose
OpenAPI description is its documentation and a CLI command whose help text is
its documentation would only gain noise from a blanket rule.

Checked over the AST, so a method is held to this whether or not its module
imports cleanly.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import TypeGuard

from support.paths import SOURCE

#: Decorators that make a method an accessor rather than an operation.
_ACCESSORS = frozenset({"property", "cached_property", "setter", "deleter"})


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_protocol(cls: ast.ClassDef) -> bool:
    return any(
        _decorator_name(base.value if isinstance(base, ast.Subscript) else base)
        == "Protocol"
        for base in cls.bases
    )


def _in_part(path: Path, part: str) -> bool:
    """Whether ``path`` sits in the skeleton part ``part``, as a module or package."""
    relative = path.relative_to(SOURCE)
    return part in relative.parts[:-1] or relative.stem == part


def _held_classes() -> Iterator[tuple[Path, ast.ClassDef]]:
    """Every public class whose methods must be documented, with its module."""
    for path in sorted(SOURCE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        service = _in_part(path, "service")
        ports = _in_part(path, "ports")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            if service or (ports and _is_protocol(node)):
                yield path, node


def _is_operation(
    node: ast.stmt,
) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """A public method that is not a property or other accessor."""
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
        and not any(_decorator_name(d) in _ACCESSORS for d in node.decorator_list)
    )


def _undocumented() -> list[str]:
    return [
        f"{path.relative_to(SOURCE)}:{method.lineno} {cls.name}.{method.name}"
        for path, cls in _held_classes()
        for method in cls.body
        if _is_operation(method) and not ast.get_docstring(method)
    ]


def test_service_and_port_methods_have_docstrings() -> None:
    """Every public method of a service class or a port ``Protocol`` says what it does.

    A ``#:`` comment above the ``def`` does not count: it is not what ``help()``,
    an editor or Sphinx shows.
    """
    offenders = _undocumented()
    assert offenders == [], "service and port methods without a docstring:\n" + (
        "\n".join(offenders)
    )


def test_the_rule_reaches_services_and_ports() -> None:
    """The walk finds both kinds of surface, so an empty result means something.

    A rename of the skeleton parts would otherwise leave the rule checking
    nothing and passing.
    """
    services = ports = 0
    for path, cls in _held_classes():
        if _is_protocol(cls) and _in_part(path, "ports"):
            ports += 1
        else:
            services += 1
    assert services > 0
    assert ports > 0
