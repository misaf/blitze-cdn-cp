"""The prose points at things that exist.

A Sphinx role (``:mod:`x```, ``:class:`x```, ``:func:`x```) is a claim about
what is here now, so every one must resolve. A plain backtick is informal and
may name the past; a reference that should be checked is written as a role.

Resolution is by import, so a re-export counts: a role may name the façade a
package publishes rather than the module that defines the object.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from support.paths import REPO_ROOT, SOURCE

#: A Sphinx cross-reference role and its target. The leading ``~`` only shortens
#: the rendered label, so it is stripped before resolution.
_XREF = re.compile(r":(?:mod|class|func|attr|meth|data|exc):`~?([A-Za-z_][\w.]*)`")

#: Only dotted targets. A bare ``:func:`derive_hosts``` is same-module shorthand
#: that needs Sphinx's context to resolve.
_MINIMUM_PARTS = 2


def _cross_references() -> dict[str, set[Path]]:
    """Every dotted Sphinx target in the source tree, with the files that claim it.

    Docstrings only: a role in a ``#:`` comment is not a Sphinx link.
    """
    found: dict[str, set[Path]] = defaultdict(set)
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            docstring = ast.get_docstring(node)
            if not docstring:
                continue
            for match in _XREF.finditer(docstring):
                target = match.group(1)
                if len(target.split(".")) >= _MINIMUM_PARTS:
                    found[target].add(path)
    return found


def _resolves(target: str) -> bool:
    """Whether ``target`` names something reachable from a running interpreter.

    The longest importable prefix is tried first and the remainder walked as
    attributes, so a package re-exporting a submodule's name cannot answer for a
    target that names the submodule.
    """
    parts = target.split(".")
    for split in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        obj: object = module
        for attribute in parts[split:]:
            if not hasattr(obj, attribute):
                return False
            obj = getattr(obj, attribute)
        return True
    return False


def test_every_docstring_cross_reference_resolves():
    """A role names something that is here, or the prose is wrong.

    Reported by the files making the claim, since the fix is rewriting the
    sentence around it.
    """
    offenders = [
        f"{target} — named in " + ", ".join(sorted(str(path) for path in paths))
        for target, paths in sorted(_cross_references().items())
        if not _resolves(target)
    ]
    assert offenders == [], "docstring cross-references that no longer resolve:\n" + (
        "\n".join(offenders)
    )


#: An inline Markdown link and its target, up to any fragment.
_MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_every_relative_markdown_link_resolves():
    """A link in a tracked document points at a file that is in the checkout.

    External links are not followed.
    """
    tracked = subprocess.run(  # noqa: S603 - fixed argument list
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],  # noqa: S607 - git from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    offenders = [
        f"{document} -> {target}"
        for document in tracked
        if (REPO_ROOT / document).exists()
        for target in _MARKDOWN_LINK.findall(
            (REPO_ROOT / document).read_text(encoding="utf-8")
        )
        if not target.startswith(("http://", "https://", "mailto:"))
        and not (REPO_ROOT / document).parent.joinpath(target).exists()
    ]
    assert offenders == [], "Markdown links to files that are not here:\n" + (
        "\n".join(offenders)
    )
