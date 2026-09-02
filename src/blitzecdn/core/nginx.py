"""Resolve package-owned Nginx resources without interpreting their content."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import AnsibleContribution, NginxContribution

__all__ = [
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "resolve_capability_environment",
    "resolve_edge_modules",
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


@dataclass(frozen=True, slots=True)
class ResolvedEdgeModule:
    """One dynamic module the installed capabilities need, and who asked."""

    plugin: str
    name: str
    objects: tuple[str, ...]
    build: bool
    probe: str


def resolve_edge_modules(
    contributions: Iterable[AnsibleContribution],
) -> tuple[ResolvedEdgeModule, ...]:
    """Compose the fleet's `load_module` set from what is installed.

    The companion to :func:`resolve_nginx_resources` for the one Nginx input
    that is not a fragment: `load_module` is a main-context directive, so the
    modules an edge loads are a single process-wide list, and core composes it
    the way it composes the role search path — ordered by plugin name so two
    controllers with the same packages resolve the same list, and by declared
    order within one plugin, which is the only ordering a capability owns.

    Two plugins declaring the *same* module is allowed when they declare it
    identically, and that is deliberate rather than lenient. njs is the case:
    it is one shared object in the base image and any number of capabilities
    may want it, and refusing the second would force one of them to declare a
    dependency on the other over a file neither of them owns. What is refused
    is the same name declared two different ways, which is not a capability
    asking for a module but two capabilities disagreeing about what it is —
    and Nginx would take whichever this happened to emit first.
    """
    resolved: list[ResolvedEdgeModule] = []
    owners: dict[str, ResolvedEdgeModule] = {}
    objects: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for module in contribution.edge_modules:
            candidate = ResolvedEdgeModule(
                plugin=contribution.plugin,
                name=module.name,
                objects=tuple(module.objects),
                build=module.build,
                probe=module.probe,
            )
            existing = owners.get(module.name)
            if existing is not None:
                if _same_module(existing, candidate):
                    continue
                raise PluginError(
                    f"Nginx module {module.name!r} is declared differently by "
                    f"plugin {existing.plugin!r} and plugin "
                    f"{contribution.plugin!r}. One module is one shared object "
                    "set built one way; two descriptions of it means an edge "
                    "loads whichever was resolved first."
                )
            for shared_object in candidate.objects:
                if shared_object in objects:
                    raise PluginError(
                        f"the shared object {shared_object!r} is loaded by both "
                        f"module {objects[shared_object]!r} and module "
                        f"{module.name!r}; Nginx refuses a module loaded twice."
                    )
                objects[shared_object] = module.name
            owners[module.name] = candidate
            resolved.append(candidate)
    return tuple(resolved)


def _same_module(left: ResolvedEdgeModule, right: ResolvedEdgeModule) -> bool:
    """Whether two declarations describe the same module, ignoring who asked."""
    return (left.objects, left.build, left.probe) == (
        right.objects,
        right.build,
        right.probe,
    )
