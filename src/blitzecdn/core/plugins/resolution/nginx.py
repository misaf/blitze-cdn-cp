"""The fragments an edge's Nginx configuration is assembled from.

One question: which contributed templates does the renderer include, in which
context, and in what order? The rules the package docstring states are applied
here to a *name* — a fragment name is claimed once across the whole
installation, because two packages contributing ``security-server.conf.j2``
would have one of them rendered and the other silently dropped.

The other half of what Nginx needs from the plugins — the ``load_module`` set —
is next door in :mod:`~blitzecdn.core.plugins.resolution.modules`, because it is
composed from a different contribution and obeys a different conflict rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import NginxContribution

__all__ = ["ResolvedNginxResource", "resolve_nginx_resources"]


_CONTEXTS = ("http", "server", "access", "upstream")


@dataclass(frozen=True, slots=True)
class ResolvedNginxResource:
    """One validated resource, ready to pass to the renderer."""

    plugin: str
    name: str
    template: Path


def resolve_nginx_resources(
    contributions: Iterable[NginxContribution],
) -> dict[str, tuple[ResolvedNginxResource, ...]]:
    """Validate paths and ownership, then order resources by plugin and name."""
    resolved: dict[str, list[ResolvedNginxResource]] = {
        context: [] for context in _CONTEXTS
    }
    owners: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        root = contribution.templates_path
        if not root.is_dir():
            raise PluginError(
                f"plugin {contribution.plugin!r} contributes the Nginx templates "
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
                    raise PluginError(
                        f"plugin {contribution.plugin!r} contributes invalid Nginx "
                        f"resource name {name!r}; names must be plain filenames"
                    )
                if name in owners:
                    raise PluginError(
                        f"Nginx resource {name!r} is contributed by both plugin "
                        f"{owners[name]!r} and plugin {contribution.plugin!r}"
                    )
                template = root / name
                if not template.is_file():
                    raise PluginError(
                        f"plugin {contribution.plugin!r} contributes Nginx resource "
                        f"{name!r}, but {template} is not a file"
                    )
                owners[name] = contribution.plugin
                resolved[context].append(
                    ResolvedNginxResource(contribution.plugin, name, template)
                )
    return {context: tuple(resources) for context, resources in resolved.items()}
