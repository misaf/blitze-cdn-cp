"""Where an installed distribution landed, and which version of it this is.

Every distribution in this workspace has to answer both questions about
*itself*, and before this module existed each one answered them on its own.
That produced two kinds of drift, and both were the silent kind.

The directory was resolved eight times by eight copies of the same twenty
lines, differing only in the distribution name inside an error string — so
two of them were never written at all. ``blitzecdn-compression`` and
``blitzecdn-http3`` reached for ``Path(__file__).with_name("roles")``, which is
the ``__file__``-relative idiom the move of the platform's Ansible into
:mod:`blitzecdn.ansible` existed to remove, and which skips the check below
entirely. Nothing failed: a `__file__`-relative path is correct in a checkout
and correct in an ordinary wheel, and wrong only where the guard would have
spoken. Three lines were simply cheaper to write than twenty, so the guard was
an opt-in and two packages opted out.

The version was worse. Ten distributions each carried
``__version__ = "3.0.0"`` as a literal — eleven times, across three different
module names — duplicating the number in their own ``pyproject.toml``. That
literal is what ``PluginMetadata.version`` reports and what ``blitzecdn
plugins`` shows an operator, so a release that bumped the manifests and missed
one of these would have described the installed capability incorrectly, at the
one moment somebody was reading it to find out what was installed. Core itself
never had the problem: ``blitzecdn/__init__.py`` has always asked
:mod:`importlib.metadata`. This module is that answer, published so that every
capability wheel can give it too.

Both functions take :mod:`importlib.resources`' *anchor* — any module or
package in the distribution, so every call site is ``__name__`` — and derive
the distribution from it. That is not a guess: this workspace's convention is
that a distribution's import package is its name with hyphens replaced by
underscores, and ``tests/architecture/test_packages.py`` enforces it in both
directions — so the mapping is a rule the suite already holds rather than a
heuristic this module invented.
"""

from __future__ import annotations

from importlib import metadata, resources
from pathlib import Path

__all__ = ["distribution_name", "distribution_version", "package_directory"]


def distribution_name(anchor: str) -> str:
    """The distribution that ships ``anchor``.

    ``anchor`` is a dotted import name; the distribution is its root with
    underscores turned back into hyphens. ``blitzecdn_cache.ansible`` ships in
    ``blitzecdn-cache``, and ``blitzecdn.docker`` in ``blitzecdn``.
    """
    return anchor.split(".")[0].replace("_", "-")


def package_directory(anchor: str, *, resolves: str) -> Path:
    """The directory ``anchor``'s own resources sit in, as a real path.

    ``anchor`` is :mod:`importlib.resources`' anchor and keeps its meaning, so
    every caller passes ``__name__`` and gets the directory beside it: a
    package resolves to its own, and a module to its containing package's.
    That is why ``blitzecdn_cache/ansible/__init__.py`` gets the ``ansible``
    directory it wants and ``blitzecdn_cache/plugin.py`` gets the package root
    the ``nginx`` templates are a child of.

    Resolved this way rather than by counting ``..`` from ``__file__``, so the
    answer is the same whether it is read from a checkout with an editable
    install or from a wheel unpacked into a virtualenv on a controller — which
    is the case that has to work, because a controller has no repository and no
    working directory to be relative to.

    The check is the reason this is a function and not an attribute. Ansible
    opens roles, plays and inventory plugins by path, Docker resolves a build
    context and a Dockerfile by path, and Nginx templates are read by path: a
    ``Traversable`` that is not a real one — a package imported from inside a
    zip — cannot be used by any of them at all. Saying so here, in one
    sentence naming the distribution, beats a role reported missing at deploy
    time or a ``TypeError`` from somewhere inside a ``docker build``
    invocation. Wheels are unpacked on install, so this is the ordinary case
    and not a fallback.

    ``resolves`` is the clause explaining *what* is opened by path, written to
    read as the middle of that sentence — "Ansible resolves its roles by
    filesystem path". It is the caller's because only the caller knows: the
    same guard protects a roles directory, a playbook, an inventory plugin
    directory, a Docker build context and a directory of Jinja templates.
    """
    directory = resources.files(anchor)
    if not isinstance(directory, Path):
        raise RuntimeError(
            f"{distribution_name(anchor)} must be installed as an unpacked "
            f"distribution: {resolves}, and this installation exposes them as "
            f"{type(directory).__name__}."
        )
    return directory


def distribution_version(anchor: str) -> str:
    """The installed version of the distribution that ships ``anchor``.

    Asked of the environment rather than written down in the source, so the
    version a plugin reports is the version that was actually installed. There
    is deliberately no fallback: a distribution that is not installed cannot
    have been discovered — an optional capability is found through the entry
    point in its own metadata, which is the same metadata read here — so
    :exc:`importlib.metadata.PackageNotFoundError` is unreachable in a running
    control plane, and a default would only let a wrong number through in the
    one situation where nobody could tell.

    Call it with ``__name__``: any module in the distribution answers for the
    whole of it.
    """
    return metadata.version(distribution_name(anchor))
