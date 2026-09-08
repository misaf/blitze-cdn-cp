"""What an installed capability asks an operator to configure.

The two halves of that question — a secret core forwards without looking at
it, and a setting core resolves and hands back typed — and the contract that
carries both. Answered by ``blitzecdn_capability_configuration``.

Why a capability declares this rather than core carrying a field for it, and
what core will and will not validate, is
``docs/decisions/0002-capability-configuration-ownership.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CapabilitySetting",
    "ConfigurationContribution",
    "EnvironmentKey",
    "SettingValue",
]


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    """One `BLITZE_*` name an installed capability claims, and its shape.

    A secret: core never looks at the value. The shape is declared here so that
    presence and length are checked at composition, before an adapter exists or
    a play could start.

    `required` is presence, and not the same question as whether the capability
    is useful — a required key is one whose absence makes the installed package
    *wrong* rather than idle, and almost every key should leave it alone.
    `minimum_bytes` is checked only when a value is present, and is for a value
    whose length is the whole of its validity. Anything richer stays the
    package's to validate.

    `summary` is what an operator is shown by the refusal that names a missing
    key and by ``blitzecdn plugins``, so write it as the sentence that would
    tell somebody where to get the value.

    See ``docs/decisions/0002-capability-configuration-ownership.md``.
    """

    name: str
    summary: str = ""
    required: bool = False
    minimum_bytes: int = 0

    def __post_init__(self) -> None:
        if self.minimum_bytes < 0:
            raise ValueError(
                f"environment key {self.name!r} declares a negative minimum length"
            )


#: What a capability's non-secret setting is allowed to be. Four types, because
#: four is what the settings that exist actually need — a count, a flag, a name
#: and a location — and because each one has an unambiguous spelling as the
#: string an environment variable or a TOML scalar arrives as. A capability
#: wanting something richer is describing a structure, and a structure belongs
#: in that capability's own file rather than in a `BLITZE_*` name.
type SettingValue = str | int | bool | Path


@dataclass(frozen=True, slots=True)
class CapabilitySetting:
    """One non-secret `BLITZE_*` name an installed capability claims.

    :class:`EnvironmentKey`'s counterpart. Together they are the whole of what a
    capability may ask an operator to configure, and having both is what keeps
    an optional capability's configuration off core's ``Settings``.

    `default` carries the type as well as the value, so a declaration cannot say
    ``int`` and mean ``"2"`` — which is why there is no ``kind`` beside it. A
    relative `Path` default resolves against the controller's state directory,
    the only sensible reading of a location a capability names before it knows
    where this controller keeps its state; an absolute one is taken as written.

    `minimum` and `maximum` apply to an ``int`` only, and are the bounds core
    can enforce without knowing what the value means. Anything richer stays the
    package's.

    `summary` is what an operator is shown by ``blitzecdn plugins``, so write it
    as the sentence that explains what changing the value would do.

    See ``docs/decisions/0002-capability-configuration-ownership.md``.
    """

    name: str
    default: SettingValue
    summary: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: Whether this setting survives a restore onto a *different* host.
    #:
    #: Almost everything does: an interval, an executable name and a CA
    #: identity are decisions an operator made, and losing them on recovery is
    #: losing configuration. What must not travel is a value describing the
    #: machine — ``blitzecdn-backup``'s own archive directory is the standing
    #: case, and restoring it would point a rebuilt controller's backups at a
    #: path belonging to the host that died.
    #:
    #: Declared here because core cannot tell. It knows which of *its own*
    #: settings describe a machine; a capability's are the capability's to
    #: classify, and a controller may have capabilities installed that this
    #: repository has never heard of.
    portable: bool = True

    def __post_init__(self) -> None:
        # `bool` first: it is a subclass of `int`, so the obvious order would
        # classify every flag as a count and let bounds be declared on one.
        if isinstance(self.default, bool) or not isinstance(self.default, int):
            if self.minimum is not None or self.maximum is not None:
                raise ValueError(
                    f"setting {self.name!r} declares bounds on a "
                    f"{type(self.default).__name__} default; minimum and "
                    "maximum apply to whole numbers only"
                )
            return
        if self.minimum is not None and self.default < self.minimum:
            raise ValueError(
                f"setting {self.name!r} defaults to {self.default}, below its "
                f"own declared minimum of {self.minimum}"
            )
        if self.maximum is not None and self.default > self.maximum:
            raise ValueError(
                f"setting {self.name!r} defaults to {self.default}, above its "
                f"own declared maximum of {self.maximum}"
            )


@dataclass(frozen=True, slots=True)
class ConfigurationContribution:
    """Everything one installed capability asks an operator to configure.

    One contract for both halves, because an operator asking "what does this
    capability need set up" is asking one question.

    Both lists are claims, and a claim is what makes ownership decidable: core
    refuses a configured ``BLITZE_*`` name that no installed capability claims.
    See ``docs/decisions/0002-capability-configuration-ownership.md``.
    """

    plugin: str
    #: Secrets. Only these are copied into Ansible's subprocess environment,
    #: they remain ``SecretStr`` throughout, and they never travel through argv
    #: or desired state.
    environment_keys: tuple[EnvironmentKey, ...] = ()
    #: Non-secrets. Resolved to the declared type and handed back through
    #: :class:`~blitzecdn.core.plugins.resolution.CapabilityConfig`.
    settings: tuple[CapabilitySetting, ...] = ()
