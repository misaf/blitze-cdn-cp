"""What an installed capability asks an operator to configure.

The two halves of that question — a secret core forwards without looking at
it, and a setting core resolves and hands back typed — and the contract that
carries both. Answered by ``blitzecdn_capability_configuration``.
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

    A name and a *shape*, rather than a bare string. The name alone answers
    only half the question: it says which package owns the name — enough to
    stop a typo, a detached package's leftover setting and two packages
    claiming one name — and says nothing about whether the capability can work
    without it or what a usable value looks like. Left to answer that half
    itself, each package answers it in its own module, in its own way, and at
    its own moment: re-spelling its key in a constant, reading it back off
    `Settings` through an untyped `getattr`, enforcing a minimum length in a
    deployment check — so a controller configured with a placeholder secret
    starts, converges, and reports the mistake only when a site first needs the
    value.

    Declaring the shape here moves each of those to the one moment they are
    all cheap: composition, before an adapter exists or a play could start.
    Core enforces the two rules it can enforce without knowing what any of
    these values *mean* — presence, and a minimum length — and hands the
    package the rest as its own typed `CapabilityConfig`.

    `required` is presence, and it is not the same question as whether the
    capability is useful. A required key is one whose absence makes the
    installed package *wrong* rather than idle, and it stops the control plane
    at startup naming the key and the package. Under Attack Mode is
    deliberately not that: a controller with no signing secret is a perfectly
    good control plane with one site setting it will refuse, which is a
    deployment check's answer and not a startup failure. Almost every key
    should leave this alone.

    `minimum_bytes` is checked only when a value is *present*, which is what
    keeps those two rules independent. It is for a value whose length is the
    whole of its validity — an HMAC secret below the hash's block size buys
    nothing, and a short one is nearly always a placeholder somebody meant to
    replace. A value with any richer rule than that stays the package's to
    validate: core cannot know what a MaxMind account id looks like, and a
    core that grew a way to describe one would be carrying the shape of a
    capability that may not be installed.

    `summary` is what an operator is shown — by the refusal that names a
    missing key, and by ``blitzecdn plugins`` — so write it as the sentence
    that would tell somebody where to get the value.
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

    :class:`EnvironmentKey`'s counterpart, and the half that was missing.
    Together they are the whole of what a capability may ask an operator to
    configure — a secret whose value core must never look at, and a setting
    whose value core resolves, type-checks and hands back.

    This class is what makes "an optional capability's configuration is not a
    field on ``Settings``" architecture rather than documentation. With only
    :class:`EnvironmentKey` to declare, the rule holds for a capability whose
    configuration happens to be a secret — ``blitzecdn-security`` — and no
    other: a non-secret setting has nowhere else to go, so ``certbot``, an ACME
    email, four renewal intervals and a backup directory stay on ``Settings``,
    each one a field the core distribution carries for a wheel that may not be
    installed, and each one unreachable for the capability that owns it without
    reading a model core owns.

    `default` carries the type as well as the value, so a declaration cannot
    say ``int`` and mean ``"2"``. That is also why there is no ``kind`` beside
    it: two ways to say the same thing is two ways for them to disagree.

    A `Path` default is resolved against the controller's state directory when
    it is relative, which is the only sensible reading of a location a
    capability names before it knows where this controller keeps its state —
    and it is exactly what core already did for its own ``backup_dir``. An
    absolute default is taken as written.

    `minimum` and `maximum` are the bounds core can enforce without knowing
    what the value *means*, and they apply to an ``int`` only. They exist
    because the settings being moved here already had them: a renewal interval
    of zero disables the job, and a negative one is nonsense that would reach
    APScheduler as a trigger it cannot build. Anything richer stays the
    package's — core cannot know that an ACME directory URL must be an ACME
    directory URL, and a core that grew a way to say so would be carrying the
    shape of a capability that may not be installed.

    `summary` is what an operator is shown by ``blitzecdn plugins``, so write
    it as the sentence that explains what changing the value would do.
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
    capability need set up" is asking one question, and ``blitzecdn plugins``
    answers it from one place. They were briefly going to be two — a new hook
    for settings beside the existing ``environment_keys`` on
    ``AnsibleContribution`` — which would have left the answer split across two
    contracts, one of them named after a subsystem that half the values never
    reach.

    Both lists are claims, and a claim is what makes ownership decidable. Core
    stages every non-core ``BLITZE_*`` name it can see, from the environment,
    from ``.env`` and from ``blitzecdn.toml``, and then refuses any that no
    installed capability claims: a typo, a setting left behind by a package
    that was detached, or a package that was never installed. Without the
    claim, all three are indistinguishable from a value that is simply being
    ignored, which is how an operator spends an afternoon on a credential that
    was reaching nothing.
    """

    plugin: str
    #: Secrets. Only these are copied into Ansible's subprocess environment,
    #: they remain ``SecretStr`` throughout, and they never travel through argv
    #: or desired state.
    environment_keys: tuple[EnvironmentKey, ...] = ()
    #: Non-secrets. Resolved to the declared type and handed back through
    #: :class:`~blitzecdn.core.plugins.resolution.CapabilityConfig`.
    settings: tuple[CapabilitySetting, ...] = ()
