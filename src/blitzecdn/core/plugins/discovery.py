"""Finding plugins, and what happens when one cannot be found or loaded.

Two sources, deliberately not one:

* **Built-ins** are module paths the composition root passes in, imported by
  name. They are not optional and not really "discovered" — the control plane
  *is* these capabilities — so resolving them through installation metadata
  would turn a broken editable install into a control plane that starts happily
  and quietly serves an empty fleet. An explicit roster fails at import, names
  the module, and can be read.

  Which capabilities that roster holds is not a fact this module knows.
  ``blitzecdn.composition.BUILTIN_PLUGINS`` is the list, because "what does this
  distribution ship" is a composition decision, and core naming the tree it
  supports is the direction this package exists to refuse.

* **External plugins** come from the ``blitzecdn.plugins`` entry-point group.
  That is the whole extension story for a separately installable package:
  ``blitzecdn-waf`` declares itself in the group, and nothing in this
  repository is edited to make it load.

The failure policy is one rule with one input — ``PluginMetadata.required``:

============================  ==========================  ====================
failure                       required plugin             optional plugin
============================  ==========================  ====================
import raises                 ``PluginError``, startup    logged with the
                              stops                       module path, skipped
registration raises           ``PluginError``             logged, skipped
no metadata hook              ``PluginError``             logged, skipped
duplicate name                ``PluginError`` either way — two plugins claiming
                              one name makes every later diagnostic ambiguous
incompatible api_version      ``PluginError``             logged, skipped
============================  ==========================  ====================

A built-in is required by definition, so any failure there is fatal. Nothing is
ever ignored silently: every skip is logged at ``warning`` with the plugin or
module that caused it, and the result carries it so ``blitzecdn plugins`` can
show an operator why something they installed is not running.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from types import ModuleType

import pluggy

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins.types import (
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
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


def _load_module(path: str) -> ModuleType:
    return import_module(path)


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
    manager: pluggy.PluginManager, candidate: object, source: str
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
    if metadata.api_version != HOOK_API_VERSION:
        raise PluginError(
            f"{source} targets hook API v{metadata.api_version}; "
            f"this control plane speaks v{HOOK_API_VERSION}"
        )
    try:
        manager.register(candidate, name=metadata.name)
    except ValueError as exc:
        raise PluginError(
            f"{source} claims the name {metadata.name!r}, which is already "
            f"registered: {exc}"
        ) from exc
    return metadata


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one discovery pass produced."""

    plugins: tuple[PluginMetadata, ...]
    rejected: tuple[PluginRejection, ...]


def register_builtins(
    manager: pluggy.PluginManager, modules: Sequence[str]
) -> tuple[PluginMetadata, ...]:
    """Register the capabilities the caller ships. Any failure is fatal.

    ``modules`` has no default. A default would be this module's own answer to
    "which capabilities exist", which is the one question core must not hold an
    opinion about — and a default is an opinion nothing has to pass to inherit.
    """
    found: list[PluginMetadata] = []
    for path in modules:
        try:
            module = _load_module(path)
        except ImportError as exc:
            raise PluginError(
                f"built-in plugin {path} could not be imported: {exc}"
            ) from exc
        metadata = register(manager, module, path)
        if not metadata.required:
            raise PluginError(
                f"built-in plugin {metadata.name} declares itself optional; "
                "a capability this distribution ships is part of the control plane"
            )
        found.append(metadata)
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
) -> Discovery:
    """Register everything advertising itself in the entry-point group.

    ``points`` is injectable so a test can exercise the real loading path
    against an entry point it built, without installing a distribution.
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
            metadata = register(manager, candidate, source)
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
    "register",
    "register_builtins",
    "register_external",
]
