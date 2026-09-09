"""The prose points at things that exist.

Roughly three lines in ten of this distribution are docstring or comment, and
CLAUDE.md asks for that: comments here carry the argument rather than restate
the line below. What nothing held until now is the other half of that bargain —
a record describing an arrangement the code no longer has is worse than no
record, and prose is the one part of the tree the compiler, ruff and mypy all
read as whitespace.

The drift this exists to catch is not hypothetical. When the `sites` capability
collapsed into `dns`, twenty-odd docstrings kept describing the arrangement in
the present tense, including the comment on `ALLOWED_CAPABILITY_DEPENDENCIES`
itself — the table that *is* the rule — and a failure message in
`test_the_composition_names_no_capability_token_at_all` that reported offenders
under a directory that had not existed for two releases.

So the rule is drawn where the codebase already draws it. A Sphinx role —
``:mod:`x```, ``:class:`x```, ``:func:`x``` — is a claim about what is here
now, and every one of the hundred-odd in the tree resolves. A plain backtick is
informal, and the house style uses it for history on purpose: "this was
`core.application.workflows`", "`sites` did it while a site was authored". That
distinction is the whole of the test. It is why there is no allow-list below and
no place for one: a reference that should be checked is written as a role, and
one that describes the past is not.

What this does *not* catch is the other half of that drift — a bare `sites` in
prose, which is most of what went stale. There is no rule that separates a
retired capability named in past tense from one named in error, so the honest
options were an allow-list of dead names or nothing, and this suite has enough
hand-maintained tables already. Write the claim you want checked as a role.

Resolution is by import rather than by reading the tree, which is what makes a
re-export count. ``:class:`~blitzecdn.core.plugins.ScheduledJob``` names the
façade the ABI publishes rather than the module the dataclass is defined in, and
that is the right thing for a docstring to say — so the check follows the same
path a reader would.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
from collections import defaultdict
from pathlib import Path

import pytest
from paths import PACKAGES, SOURCE

#: A Sphinx cross-reference role and the target inside it. The leading ``~`` is
#: display sugar — it shortens the rendered label to the last component and says
#: nothing about the target — so it is stripped before resolution.
_XREF = re.compile(r":(?:mod|class|func|attr|meth|data|exc):`~?([A-Za-z_][\w.]*)`")

#: Only the dotted targets. A bare ``:func:`derive_hosts``` is Sphinx's
#: same-module shorthand: resolving it needs the context Sphinx has and this
#: test does not, and guessing at it would produce failures that are about the
#: guess. The dotted ones are unambiguous, and they are the ones that go stale —
#: a rename moves the module, and the short form has no module in it to be wrong.
_MINIMUM_PARTS = 2


def _source_roots() -> list[Path]:
    """This distribution's source, and every optional wheel's.

    A packaged capability's docstrings drift for exactly the reason core's do,
    and ``blitzecdn-backup`` had a stale one. Discovered rather than listed, for
    the reason :func:`~paths.optional_packages` is.
    """
    return [SOURCE, *sorted(PACKAGES.glob("*/src"))]


def _import_root(root: Path) -> str:
    """The import package a source root ships.

    ``src/blitzecdn`` is core's own and is the root itself; a wheel's ``src/``
    holds exactly one package, which the workspace layout rules already hold.
    Derived from the directory rather than spelled, so this stays out of the way
    of the rule that the control-plane suite names no optional package.
    """
    if root == SOURCE:
        return root.name
    return next(child.name for child in sorted(root.iterdir()) if child.is_dir())


def _cross_references(root: Path) -> dict[str, set[Path]]:
    """Every dotted Sphinx target under ``root``, with the files that claim it.

    Docstrings only, and only the four node types that carry one. A role
    written in a ``#:`` comment is not read: comments are not addressable by
    Sphinx, so a role in one is decoration rather than a link, and holding it to
    the same standard would be holding it to a standard nothing enforces
    downstream either.
    """
    found: dict[str, set[Path]] = defaultdict(set)
    for path in sorted(root.rglob("*.py")):
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

    A dotted target is some importable prefix followed by attribute access, and
    which is which cannot be read off the string: ``a.b.c`` may be a module, or
    a class in ``a.b``, or a method on a class in ``a``. So the longest
    importable prefix is tried first and the remainder walked as attributes —
    the order matters, because a package that re-exports a submodule's name
    would otherwise answer for a target that names the submodule.
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


@pytest.mark.parametrize("root", _source_roots(), ids=lambda root: _import_root(root))
def test_every_docstring_cross_reference_resolves(root: Path):
    """A role names something that is here, or the prose is wrong.

    One case per source root, so a detached capability's prose is a visible skip
    in the core-only run rather than a hundred failures about imports that were
    never going to succeed. Core's own root is always installed, so the case
    that matters most cannot skip.

    Failing with the file that makes the claim rather than with the target
    alone, because a stale reference is fixed by rewriting the sentence around
    it — and the same dead name is usually written in several places at once,
    which is the shape this reports.
    """
    if importlib.util.find_spec(_import_root(root)) is None:
        pytest.skip(f"{root.parent.name} is not installed")

    offenders = [
        f"{target} — named in " + ", ".join(sorted(str(path) for path in paths))
        for target, paths in sorted(_cross_references(root).items())
        if not _resolves(target)
    ]
    assert offenders == [], "docstring cross-references that no longer resolve:\n" + (
        "\n".join(offenders)
    )
