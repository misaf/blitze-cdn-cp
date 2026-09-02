"""Resolve package-owned Nginx resources without interpreting their content."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import AnsibleContribution, NginxContribution

__all__ = [
    "ResolvedNginxResource",
    "resolve_capability_environment",
    "resolve_nginx_resources",
]

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


def resolve_capability_environment(
    contributions: Iterable[AnsibleContribution],
    configured: Mapping[str, SecretStr],
) -> dict[str, SecretStr]:
    """Return only explicitly owned values and reject collisions or typos."""
    owners: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for key in contribution.environment_keys:
            if not key.startswith("BLITZE_"):
                raise PluginError(
                    f"plugin {contribution.plugin!r} claims environment key {key!r}, "
                    "which must start with 'BLITZE_'"
                )
            if key in owners:
                raise PluginError(
                    f"environment key {key!r} is claimed by both plugin "
                    f"{owners[key]!r} and plugin {contribution.plugin!r}"
                )
            owners[key] = contribution.plugin
    unknown = sorted(set(configured) - owners.keys())
    if unknown:
        raise PluginError(
            "unknown capability environment configuration: " + ", ".join(unknown)
        )
    return {key: configured[key] for key in sorted(configured) if key in owners}
