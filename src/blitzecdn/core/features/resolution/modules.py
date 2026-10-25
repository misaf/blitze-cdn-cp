"""The dynamic modules an edge's Nginx loads.

The opposite conflict rule to fragments: any number of capabilities may
declare one module as long as they declare it identically — njs is one shared
object several may want. One name described two ways, or one shared object
loaded twice, is refused. Composed from ``AnsibleContribution`` because a
module is a property of the image.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import AnsibleContribution

__all__ = ["ResolvedEdgeModule", "resolve_edge_modules"]


@dataclass(frozen=True, slots=True)
class ResolvedEdgeModule:
    """One dynamic module the installed capabilities need, and who asked."""

    feature: str
    name: str
    objects: tuple[str, ...]
    build: bool
    probe: str


def resolve_edge_modules(
    contributions: Iterable[AnsibleContribution],
) -> tuple[ResolvedEdgeModule, ...]:
    """Compose the fleet's `load_module` list, ordered by feature, then as declared."""
    resolved: list[ResolvedEdgeModule] = []
    owners: dict[str, ResolvedEdgeModule] = {}
    objects: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.feature):
        for module in contribution.edge_modules:
            candidate = ResolvedEdgeModule(
                feature=contribution.feature,
                name=module.name,
                objects=tuple(module.objects),
                build=module.build,
                probe=module.probe,
            )
            existing = owners.get(module.name)
            if existing is not None:
                if _same_module(existing, candidate):
                    continue
                raise FeatureError(
                    f"Nginx module {module.name!r} is declared differently by "
                    f"feature {existing.feature!r} and feature "
                    f"{contribution.feature!r}. One module is one shared object "
                    "set built one way; two descriptions of it means an edge "
                    "loads whichever was resolved first."
                )
            for shared_object in candidate.objects:
                if shared_object in objects:
                    raise FeatureError(
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
