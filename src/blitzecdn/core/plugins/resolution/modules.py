"""The dynamic modules an edge's Nginx loads, composed from what is installed.

Separate from :mod:`~blitzecdn.core.plugins.resolution.nginx` although both
end up in the same configuration, because the two obey opposite conflict
rules and that difference is the whole content of each.

A fragment name is claimed *once*: two packages contributing it is a mistake
whichever way it resolves. A module may be declared by any number of
capabilities as long as they declare it identically — njs is one shared object
in the base image, and refusing the second claimant would force one capability
to depend on another over a file neither of them owns. What is refused is one
name described two ways, and one shared object loaded twice.

It is composed from ``AnsibleContribution`` rather than from
``NginxContribution`` because a module is a property of the *image* a
capability needs, which is the same declaration that carries its roles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import AnsibleContribution

__all__ = ["ResolvedEdgeModule", "resolve_edge_modules"]


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
