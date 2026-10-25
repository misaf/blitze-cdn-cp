"""The fragments an edge's Nginx configuration is assembled from.

A fragment name is claimed once: two features contributing the same file
would have one rendered and the other silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import NginxContribution

__all__ = ["ResolvedNginxResource", "resolve_nginx_resources"]


_CONTEXTS = ("http", "server", "access", "upstream", "module")


@dataclass(frozen=True, slots=True)
class ResolvedNginxResource:
    """One validated resource, ready to pass to the renderer."""

    feature: str
    name: str
    template: Path


def resolve_nginx_resources(
    contributions: Iterable[NginxContribution],
) -> dict[str, tuple[ResolvedNginxResource, ...]]:
    """Validate paths and ownership, then order resources by feature and name."""
    resolved: dict[str, list[ResolvedNginxResource]] = {
        context: [] for context in _CONTEXTS
    }
    owners: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.feature):
        root = contribution.templates_path
        if not root.is_dir():
            raise FeatureError(
                f"feature {contribution.feature!r} contributes the Nginx templates "
                f"directory {root}, which does not exist"
            )
        for context in _CONTEXTS:
            names = getattr(contribution, f"{context}_fragments")
            for name in names:
                relative = Path(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.name != name
                ):
                    raise FeatureError(
                        f"feature {contribution.feature!r} contributes invalid Nginx "
                        f"resource name {name!r}; names must be plain filenames"
                    )
                if name in owners:
                    raise FeatureError(
                        f"Nginx resource {name!r} is contributed by both feature "
                        f"{owners[name]!r} and feature {contribution.feature!r}"
                    )
                template = root / name
                if not template.is_file():
                    raise FeatureError(
                        f"feature {contribution.feature!r} contributes Nginx resource "
                        f"{name!r}, but {template} is not a file"
                    )
                owners[name] = contribution.feature
                resolved[context].append(
                    ResolvedNginxResource(contribution.feature, name, template)
                )
    return {context: tuple(resources) for context, resources in resolved.items()}
