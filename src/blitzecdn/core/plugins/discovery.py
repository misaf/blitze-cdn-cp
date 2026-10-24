"""Finding features and plugins, and what happens when one cannot be loaded.

Two sources, deliberately not one:

* **Built-in features** are modules the composition root names, each exporting
  ``FEATURES``: typed :class:`~blitzecdn.core.plugins.Feature` declarations.
  They are not optional and not really "discovered" — the control plane *is*
  these features — so resolving them through installation metadata would turn a
  broken editable install into a control plane that starts happily and quietly
  serves an empty fleet. An explicit roster fails at import, names the module,
  and can be read. Which modules it holds is the composition root's answer.

* **External plugins** come from the ``blitzecdn.plugins`` entry-point group and
  implement the hooks in :mod:`blitzecdn.core.plugins.hooks`. That is the whole
  extension story for a separately installable package: ``blitzecdn-waf``
  declares itself in the group, and nothing in this repository is edited.

An external plugin that fails to import, fails to register, gives no metadata,
claims a name already taken, or targets an unsupported hook API is skipped and
recorded with its reason. An installation that cannot run without it says so
through ``required_capabilities``, which then refuses to start.

A built-in is required by definition, so any failure there is fatal. Nothing is
ever ignored silently: every skip is logged at ``warning`` with the plugin or
module that caused it, and the result carries it so ``blitzecdn plugins`` can
show an operator why something they installed is not running.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points

import pluggy

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import (
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
    SUPPORTED_HOOK_API_VERSIONS,
    Feature,
    PluginMetadata,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PluginRejection:
    """A plugin that did not load, and why — kept rather than discarded.

    An optional plugin that fails is skipped, but "skipped" must still be
    answerable. An operator who installed ``blitzecdn-waf`` and sees no WAF
    routes needs to read the reason somewhere, and a log line that scrolled
    past at startup is not somewhere.
    """

    source: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}: {self.reason}"


def _metadata_of(manager: pluggy.PluginManager, plugin: object) -> PluginMetadata:
    """Read one plugin's own metadata, ignoring what every other plugin said.

    Read through the registered hook implementations rather than by reaching
    for a module attribute, so a plugin is free to use
    ``@hookimpl(specname=...)`` and name its function whatever it likes.
    """
    for implementation in manager.hook.blitzecdn_plugin_metadata.get_hookimpls():
        if implementation.plugin is plugin:
            found = implementation.function()
            if isinstance(found, PluginMetadata):
                return found
            raise PluginError(
                "blitzecdn_plugin_metadata must return a PluginMetadata, "
                f"not {type(found).__name__}"
            )
    raise PluginError("no blitzecdn_plugin_metadata hook")


def register(
    manager: pluggy.PluginManager,
    candidate: object,
    source: str,
    *,
    reserved: Collection[str] = (),
) -> PluginMetadata:
    """Register one plugin under the name it claims, or raise ``PluginError``.

    Two passes, because a plugin's identity is something it states in a hook
    and hooks are only callable once it is registered. It goes in under a
    provisional name, is asked who it is, and goes back in under that answer —
    so a duplicate collides on the identity a plugin claims rather than on the
    module path it happened to be imported from, and a half-registered plugin
    never survives a failure: its hooks would be callable while nothing could
    attribute their results.
    """
    try:
        manager.register(candidate)
    except ValueError as exc:
        raise PluginError(f"{source} could not be registered: {exc}") from exc
    try:
        metadata = _metadata_of(manager, candidate)
    finally:
        manager.unregister(candidate)
    _check_api_version(metadata, source)
    if metadata.name in reserved:
        raise PluginError(
            f"{source} claims the name {metadata.name!r}, which a built-in "
            "feature already uses"
        )
    try:
        manager.register(candidate, name=metadata.name)
    except ValueError as exc:
        raise PluginError(
            f"{source} claims the name {metadata.name!r}, which is already "
            f"registered: {exc}"
        ) from exc
    return metadata


def _check_api_version(metadata: PluginMetadata, source: str) -> None:
    if metadata.api_version not in SUPPORTED_HOOK_API_VERSIONS:
        supported = ", ".join(
            f"v{version}" for version in sorted(SUPPORTED_HOOK_API_VERSIONS)
        )
        raise PluginError(
            f"{source} targets hook API v{metadata.api_version}; "
            f"this control plane speaks v{HOOK_API_VERSION} and accepts {supported}"
        )


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one discovery pass produced."""

    plugins: tuple[PluginMetadata, ...]
    rejected: tuple[PluginRejection, ...]


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
            raise PluginError(
                f"built-in feature module {path} could not be imported: {exc}"
            ) from exc
        declared = getattr(module, "FEATURES", None)
        if not isinstance(declared, tuple) or not declared:
            raise PluginError(f"{path} must export FEATURES, a tuple of Feature")
        for feature in declared:
            if not isinstance(feature, Feature):
                raise PluginError(
                    f"{path} exports a {type(feature).__name__} in FEATURES "
                    "where a Feature was expected"
                )
            _check_api_version(feature.metadata, path)
            if not feature.metadata.required:
                raise PluginError(
                    f"built-in feature {feature.name} declares itself optional; "
                    "a feature this distribution ships is part of the control "
                    "plane"
                )
            if feature.name in names:
                raise PluginError(
                    f"{path} and {names[feature.name]} both declare a feature "
                    f"named {feature.name!r}"
                )
            names[feature.name] = path
            found.append(feature)
    return tuple(found)


def _external_entry_points(group: str) -> Iterator[EntryPoint]:
    # Sorted by name so two installed plugins register in an order that does
    # not depend on how the filesystem happened to list site-packages.
    yield from sorted(entry_points(group=group), key=lambda point: point.name)


def register_external(
    manager: pluggy.PluginManager,
    *,
    group: str = ENTRY_POINT_GROUP,
    points: Iterable[EntryPoint] | None = None,
    reserved: Collection[str] = (),
) -> Discovery:
    """Register everything advertising itself in the entry-point group.

    ``points`` is injectable so a test can exercise the real loading path
    against an entry point it built, without installing a distribution.
    ``reserved`` is the built-in feature names, which no plugin may claim.
    """
    found: list[PluginMetadata] = []
    rejected: list[PluginRejection] = []
    for point in points if points is not None else _external_entry_points(group):
        source = f"{point.name} ({point.value})"
        try:
            candidate = point.load()
        # Blind on purpose: this executes a third party's module body, which
        # can raise anything at all. Narrowing it would turn "that plugin is
        # broken, here is why" back into a traceback out of a control plane
        # that has nothing to do with the fault.
        except Exception as exc:  # noqa: BLE001
            rejected.append(PluginRejection(source, f"import failed: {exc}"))
            _LOGGER.warning("plugin %s could not be imported: %s", source, exc)
            continue
        try:
            metadata = register(manager, candidate, source, reserved=reserved)
        except PluginError as exc:
            rejected.append(PluginRejection(source, str(exc)))
            _LOGGER.warning("plugin %s was not registered: %s", source, exc)
            continue
        if metadata.required:
            # An external plugin may declare itself required — an operator who
            # installed a WAF and cannot serve traffic without it wants the
            # node to refuse to start rather than to come up unprotected.
            _LOGGER.info("required external plugin %s registered", metadata.name)
        found.append(metadata)
    return Discovery(tuple(found), tuple(rejected))


__all__ = [
    "Discovery",
    "PluginRejection",
    "load_features",
    "register",
    "register_external",
]
