"""Loading the built-in features, and refusing to start without one.

Each module the composition root names exports ``FEATURES``, a tuple of
:class:`~blitzecdn.core.features.Feature`. The roster is explicit rather than
discovered from installation metadata, so a broken install fails at import and
names the module instead of starting with an empty fleet.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import Feature

__all__ = ["load_features"]


def load_features(modules: Sequence[str]) -> tuple[Feature, ...]:
    """Import the named feature modules. Any failure is fatal.

    No default: which features exist is the composition root's answer, not core's.
    """
    found: list[Feature] = []
    names: dict[str, str] = {}
    for path in modules:
        try:
            module = import_module(path)
        except ImportError as exc:
            raise FeatureError(
                f"built-in feature module {path} could not be imported: {exc}"
            ) from exc
        declared = getattr(module, "FEATURES", None)
        if not isinstance(declared, tuple) or not declared:
            raise FeatureError(f"{path} must export FEATURES, a tuple of Feature")
        for feature in declared:
            if not isinstance(feature, Feature):
                raise FeatureError(
                    f"{path} exports a {type(feature).__name__} in FEATURES "
                    "where a Feature was expected"
                )
            if feature.name in names:
                raise FeatureError(
                    f"{path} and {names[feature.name]} both declare a feature "
                    f"named {feature.name!r}"
                )
            names[feature.name] = path
            found.append(feature)
    return tuple(found)
