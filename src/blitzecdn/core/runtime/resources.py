"""Locate the package's filesystem resources and its installed version."""

from __future__ import annotations

from importlib import metadata, resources
from pathlib import Path

__all__ = ["distribution_name", "distribution_version", "package_directory"]


def distribution_name(anchor: str) -> str:
    """The distribution owning an import root; every built-in belongs to blitzecdn."""
    return anchor.split(".")[0].replace("_", "-")


def package_directory(anchor: str, *, resolves: str) -> Path:
    """The real directory ``anchor``'s resources sit in; pass ``__name__``.

    Resolved through :mod:`importlib.resources` so it works from an installed
    package with no checkout. Ansible, Docker and Nginx all open these by path, so
    a resource that is not a real directory — a zip import — is refused here with
    ``resolves`` completing the sentence ("Ansible resolves its roles by
    filesystem path"), rather than failing later inside a play or a build.
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
    """The installed version of the distribution shipping ``anchor``; no fallback."""
    return metadata.version(distribution_name(anchor))
