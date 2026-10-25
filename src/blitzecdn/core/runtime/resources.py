"""Locate filesystem resources and owning distribution versions.

Application anchors all belong to ``blitzecdn``. External extension anchors may
belong to another distribution; neither needs a checkout-relative resource path.
"""

from __future__ import annotations

from importlib import metadata, resources
from pathlib import Path

__all__ = ["distribution_name", "distribution_version", "package_directory"]


def distribution_name(anchor: str) -> str:
    """Distribution owning an import root; built-ins all belong to blitzecdn."""
    return anchor.split(".")[0].replace("_", "-")


def package_directory(anchor: str, *, resolves: str) -> Path:
    """The directory ``anchor``'s own resources sit in, as a real path.

    ``anchor`` is :mod:`importlib.resources`' anchor and keeps its meaning, so
    every caller passes ``__name__`` and gets the directory beside it: a
    package resolves to its own, and a module to its containing package's.
    The cache Ansible package gets its own directory; the cache registration
    module gets its containing package, where its Nginx templates live.

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
    version a feature reports is the version that was actually installed. There
    is deliberately no fallback: the code asking is running from the installed
    distribution, so
    :exc:`importlib.metadata.PackageNotFoundError` is unreachable in a running
    control plane, and a default would only let a wrong number through in the
    one situation where nobody could tell.

    Call it with ``__name__``: any module in the distribution answers for the
    whole of it.
    """
    return metadata.version(distribution_name(anchor))
