"""Compose the site policy differently for one test, and put the real one back.

``blitzecdn.composition`` composes the site policy once, at import, and that
composition is process-wide. A test that needs an extra policy contribution
reseals it, which changes more than one value:

- ``SitePolicy._schema``, the contributions every site policy is composed from;
- ``__composed_cache__`` on each static ``ComposedModel`` (``SitePolicy`` and
  every model that nests it), which keeps one generated model per schema ever
  sealed, so a temporary schema would stay reachable for the rest of the run.

:func:`temporary_policy_composition` snapshots all of it and restores it in a
``finally``, so the built-in composition is back after a passing test, a failed
assertion, or a composition that raises. Use the ``policy_composition``
fixture, or the context manager where only part of a test is recomposed.
Never assign ``SitePolicy._schema`` or call ``compose_registry`` directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from blitzecdn.composition import BUILTIN_REGISTRY, compose_registry
from blitzecdn.core.domain.composed import ComposedModel
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.features import Feature, FeatureRegistry, PolicyContribution
from blitzecdn.core.features.resolution import resolve_policy_schema

__all__ = [
    "PolicyComposer",
    "composition_state",
    "policy_composition",
    "temporary_policy_composition",
]

_CACHE = "__composed_cache__"
_ABSENT = object()


def _composed_models() -> list[type[ComposedModel]]:
    """Every static ``ComposedModel`` loaded now: those that keep a cache.

    Generated models are skipped; ``composed`` caches on the static class.
    """
    found: list[type[ComposedModel]] = []
    pending: list[type[ComposedModel]] = [ComposedModel]
    while pending:
        model = pending.pop()
        if not model.__dict__.get("__composed__", False):
            found.append(model)
            pending.extend(model.__subclasses__())
    return found


def composition_state() -> tuple[object, dict[type[ComposedModel], object]]:
    """The policy schema and each model's own generated-model cache, by identity."""
    return SitePolicy._schema, {
        model: model.__dict__.get(_CACHE, _ABSENT) for model in _composed_models()
    }


def _restore(state: tuple[object, dict[type[ComposedModel], object]]) -> None:
    schema, caches = state
    SitePolicy._schema = schema  # type: ignore[assignment]
    for model in _composed_models():
        current: dict[int, tuple[object, type]] = model.__dict__.get(_CACHE) or {}
        saved = caches.get(model, _ABSENT)
        restored = {} if saved is _ABSENT else dict(saved)  # type: ignore[call-overload]
        # A model first generated for the restored schema during the test is
        # the one later values must share, so it is kept; any other is dropped.
        restored.update(
            (key, entry) for key, entry in current.items() if entry[0] is schema
        )
        if restored:
            type.__setattr__(model, _CACHE, restored)
        elif _CACHE in model.__dict__:
            type.__delattr__(model, _CACHE)


@dataclass(frozen=True, slots=True)
class PolicyComposer:
    """Recompose the policy inside :func:`temporary_policy_composition`."""

    def features(self, *extra: Feature) -> FeatureRegistry:
        """The built-in features plus ``extra``, with the policy composed from them."""
        return compose_registry((*BUILTIN_REGISTRY.features, *extra))

    def contributions(self, *extra: PolicyContribution, builtin: bool = True) -> None:
        """Seal the policy from ``extra``, beside the built-in contributions or not."""
        base = BUILTIN_REGISTRY.policy_contributions() if builtin else ()
        SitePolicy.seal(resolve_policy_schema((*base, *extra)))

    def uncompose(self) -> None:
        """Leave the policy uncomposed, as before ``blitzecdn.composition`` loads."""
        SitePolicy._schema = None


@contextmanager
def temporary_policy_composition() -> Iterator[PolicyComposer]:
    """Recompose the policy freely inside; every affected value is restored after."""
    state = composition_state()
    try:
        yield PolicyComposer()
    finally:
        _restore(state)


@pytest.fixture
def policy_composition() -> Iterator[PolicyComposer]:
    """A :class:`PolicyComposer` whose changes are undone when the test ends."""
    with temporary_policy_composition() as composer:
        yield composer
