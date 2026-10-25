"""Loading the built-in features, and what happens when one cannot be loaded.

Built-in features are modules the composition root names, each exporting
``FEATURES``: typed :class:`~blitzecdn.core.features.Feature` declarations.
They are not optional and not really "discovered" — the control plane *is*
these features — so resolving them through installation metadata would turn a
broken editable install into a control plane that starts happily and quietly
serves an empty fleet. An explicit roster fails at import, names the module,
and can be read. Which modules it holds is the composition root's answer.

There is no third-party extension mechanism. When one is needed it appends
to the same roster; see
docs/decisions/0021-capability-roster-and-process-composition.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module

from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features.types import Feature

__all__ = ["load_features"]


def load_features(modules: Sequence[str]) -> tuple[Feature, ...]:
    """Import the features the caller ships. Any failure is fatal.

    ``modules`` has no default. A default would be this module's own answer to
    "which features exist", which is the one question core must not hold an
    opinion about.
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
