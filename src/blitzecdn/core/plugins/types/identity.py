"""Who a plugin is, and which hook contract it speaks.

The registration half of the ABI: read before any contribution is, and the
only part of it a plugin that contributes nothing still has to answer.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "SUPPORTED_HOOK_API_VERSIONS",
    "PluginMetadata",
]


#: The pluggy project name. It prefixes every hook, which is what lets one
#: manager host hooks from packages that never heard of each other.
PROJECT_NAME = "blitzecdn"

#: The entry-point group an external distribution advertises itself in.
ENTRY_POINT_GROUP = "blitzecdn.plugins"

#: The hook contract this control plane speaks. Bumped only when a hookspec
#: changes shape in a way an existing implementation cannot survive.
HOOK_API_VERSION = 1

#: Every contract version this control plane still accepts a plugin for.
#:
#: A set rather than a comparison against `HOOK_API_VERSION`, because the gate
#: used to be `!=` and that made bumping the contract a same-day fork of every
#: wheel in existence: v2 core, and every plugin declaring v1 refused at once,
#: with no release in which an author could support both. Widening this to
#: `{1, 2}` is what a deprecation window *is* — v1 plugins keep loading while
#: their authors move, and dropping 1 later is a second, separate decision that
#: shows up in this line.
#:
#: `pip` already refuses a wheel whose `blitzecdn>=3.0.0,<4` cannot be
#: satisfied, so this is the second lock rather than the first. It earns its
#: place on the installs that never went through a resolver — an editable
#: checkout, a vendored tree — where the alternative is an AttributeError
#: inside a hook call that names no plugin.
SUPPORTED_HOOK_API_VERSIONS = frozenset({1})


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Who a plugin is, and what happens when it cannot be loaded.

    `required` is the whole of the failure policy. A required plugin that
    fails to import or register stops the process at startup, because the
    control plane without it is not a degraded control plane but a wrong one —
    a `dns` that did not load would silently render an empty fleet. An optional
    plugin's failure is reported with its name and the process continues.
    """

    name: str
    version: str
    #: The hook contract this plugin was *written against*, as a literal.
    #:
    #: Required, and deliberately so. It defaulted to `HOOK_API_VERSION` —
    #: core's own constant, read at the plugin's import time — which meant a
    #: plugin that said nothing always agreed with whatever core it was loaded
    #: into, however old the hooks it implements. Every one of the twenty-three
    #: plugins in this workspace omitted it, so the check had never once been
    #: able to fail. The author who was explicit and pinned `1` was the only
    #: one it could ever refuse: exactly backwards.
    #:
    #: Write the number, not `HOOK_API_VERSION`. Importing the constant
    #: reintroduces the same defect one level up —
    #: `test_a_plugin_states_the_hook_contract_it_was_written_against` refuses
    #: it for the plugins in this workspace, and it is why this field cannot
    #: have a default: there is no value core can supply that means "whatever
    #: the author had in front of them".
    api_version: int
    required: bool = False
    summary: str = ""
    #: The capability tokens this plugin supplies, for configuration that has
    #: to say what it depends on. Empty means "just my own name", which is the
    #: right answer for almost every plugin: `backup` provides `backup`.
    #:
    #: It is separate from `name` because the two answer different questions.
    #: `name` identifies the *plugin* — it is how a duplicate is detected and a
    #: failure attributed — while a token names a *capability*, which is what a
    #: site or a fleet setting actually depends on. One plugin may supply
    #: several, and a replacement implementation may supply a token another
    #: package used to, under its own name.
    provides: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"plugin name {self.name!r} must be alphanumeric")
        for token in self.provides:
            if not token or not token.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"capability {token!r} must be alphanumeric")

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token this plugin answers for, its name included."""
        return self.provides | {self.name}
