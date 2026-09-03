"""What an operator configured, resolved into what each capability may read.

The one resolver here that is not about a *fleet* value. The role search path,
the slot lists, the module set and the fragment set are all single
process-wide answers core composes and hands to an adapter; a capability's
configuration is composed once and then handed back **scoped**, so that a
package reads the keys it declared and cannot see another's credential.

That scoping is why the collision rules in the package docstring are enforced
across every contribution before a single value is read: names share one
``BLITZE_*`` namespace, and two capabilities claiming one name is two packages
reading a value either of them may reconfigure for the other's sake.

Secrets and settings are both here because they arrive together and are
refused together, and are separated only once they have owners — a secret is
forwarded to Ansible and never written to a committed file, a setting is
resolved for the controller and always has a value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, PluginError
from blitzecdn.core.plugins.types import (
    CapabilitySetting,
    ConfigurationContribution,
    EnvironmentKey,
    SettingValue,
)

__all__ = [
    "CapabilityConfig",
    "ResolvedCapabilityEnvironment",
    "resolve_capability_environment",
    "resolve_plugin_configuration",
]


@dataclass(frozen=True, slots=True)
class CapabilityConfig:
    """One installed package's own configuration, and nothing else's.

    A capability that needs a credential used to reach for
    ``Settings.capability_environment`` itself — an untyped ``getattr`` against
    a model core owns, returning every claimed key in the installation, out of
    which the package picked its own by re-spelling the name. It worked, and
    every part of it was a copy waiting to drift: the name in two places, the
    read in a helper each package wrote again, and no answer at all to "what
    does this capability need" for anything that had to ask.

    This is that read, done once by core and handed back scoped: the keys *this
    plugin declared*, resolved from the merged environment, with an unset one
    present as an empty secret rather than absent. A name the package did not
    declare is refused rather than returned empty, because the two mistakes
    look identical at the call site and only one of them is a typo.
    """

    plugin: str
    values: Mapping[str, SecretStr]
    #: The non-secret half, already coerced to each setting's declared type.
    #: Separate from `values` because the two are read differently and shown
    #: differently: a secret is only ever asked whether it is *set*, while a
    #: setting is asked for its value and has one whether or not an operator
    #: supplied it.
    settings: Mapping[str, SettingValue] = field(default_factory=dict)

    def secret(self, name: str) -> SecretStr:
        """This key's value, empty when the controller has not set it."""
        if name not in self.values:
            raise PluginError(
                f"plugin {self.plugin!r} reads the environment key {name!r}, "
                "which it does not declare. A secret is claimed once, in its "
                "`ConfigurationContribution.environment_keys`, and read by "
                "that name."
            )
        return self.values[name]

    def is_set(self, name: str) -> bool:
        """Whether the controller supplied a value for this declared key."""
        return bool(self.secret(name).get_secret_value())

    def _setting(self, name: str, kind: type) -> SettingValue:
        """One declared setting, checked against the type the caller expects.

        The check is not redundant with the coercion that produced the value.
        That one held the value to the *declaration*; this one holds the
        declaration to the *call site*, and the two are written in different
        files by different people. `integer()` on a setting somebody later
        redeclared as a path is a mistake worth a sentence rather than a
        `TypeError` several frames away.
        """
        if name not in self.settings:
            raise PluginError(
                f"plugin {self.plugin!r} reads the setting {name!r}, which it "
                "does not declare. A setting is claimed once, in its "
                "`ConfigurationContribution.settings`, and read by that name."
            )
        value = self.settings[name]
        # `bool` before `int`, as everywhere this type is inspected.
        matches = (
            isinstance(value, bool) if kind is bool else isinstance(value, kind)
        ) and not (kind is int and isinstance(value, bool))
        if not matches:
            raise PluginError(
                f"plugin {self.plugin!r} reads {name!r} as {kind.__name__}, "
                f"but declares it as {type(value).__name__}"
            )
        return value

    def text(self, name: str) -> str:
        """A declared `str` setting. Empty string when nothing was supplied."""
        return cast(str, self._setting(name, str))

    def integer(self, name: str) -> int:
        """A declared `int` setting, already inside its declared bounds."""
        return cast(int, self._setting(name, int))

    def flag(self, name: str) -> bool:
        """A declared `bool` setting."""
        return cast(bool, self._setting(name, bool))

    def path(self, name: str) -> Path:
        """A declared `Path` setting, resolved against the state directory."""
        return cast(Path, self._setting(name, Path))


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityEnvironment:
    """Every claimed key, once flat for Ansible and once scoped per plugin.

    Two shapes of one answer, because there are two consumers and they need
    opposite things. ``environment`` is what ``PlaybookExecutor`` copies into
    the subprocess, where a role reads its own name with ``lookup('env', ...)``
    and the flatness *is* the interface. ``configs`` is what a package reads on
    the controller, where seeing another capability's credential would be the
    thing this whole seam exists to prevent.
    """

    environment: Mapping[str, SecretStr]
    configs: Mapping[str, CapabilityConfig]

    def for_plugin(self, plugin: str) -> CapabilityConfig:
        """This plugin's configuration; empty if it declared no keys.

        Empty rather than an error, because declaring no keys is the normal
        case and a package should not have to know whether it is one. A
        mistyped plugin name is caught a line later instead: every read against
        the empty config names a key that config does not declare.
        """
        return self.configs.get(plugin) or CapabilityConfig(plugin, {})


def resolve_capability_environment(
    contributions: Iterable[ConfigurationContribution],
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> ResolvedCapabilityEnvironment:
    """Resolve claimed configuration, and refuse what cannot be worked with.

    Four refusals, and each one is here because the alternative is a failure
    much later that names nothing useful. A key without the ``BLITZE_`` prefix
    is a package claiming a name the controller never collects. A key claimed
    twice is two packages reading one value, either of which may be reconfigured
    for the other's sake. A configured key nobody claims is a typo, a setting
    left behind by a detached package, or a package that was never installed —
    and silently ignoring it is how an operator spends an afternoon on a
    credential that was reaching nothing.

    The fourth is the value itself: a declared-required key that is absent, a
    present one shorter than the length its capability declared usable, or a
    setting that cannot be read as the type it was declared with. All are
    ``ConfigurationError`` rather than ``PluginError``, and the distinction is
    the one the exception hierarchy already draws — nothing is wrong with the
    installed package, and the fix is a value the operator can change.

    Secrets and settings share one namespace deliberately. They arrive as one
    set of `BLITZE_*` names and an operator sets them the same way, so a
    capability claiming one name as both — or two capabilities disagreeing
    about which kind a name is — is the same collision as any other and is
    reported as one.

    Only *secrets* are forwarded to Ansible. A setting is resolved for the
    controller, and a role that needs the same value reads it from the desired
    state document or from its own role defaults, which is where non-secret
    fleet policy already lives.

    The two sources are kept apart on purpose. ``configured`` is the process
    environment and the controller's ``.env``, which is 0600 and uncommitted;
    ``from_file`` is ``blitzecdn.toml``, which is neither. A setting may come
    from either, the environment winning; a *secret* may come only from the
    first, so a package cannot document its signing key into a file an
    operator would commit. That rule used to be enforced by the TOML reader
    refusing every name it did not recognise — which also made every
    non-secret capability setting unconfigurable there.
    """
    owners: dict[str, str] = {}
    declared: dict[str, ConfigurationContribution] = {}
    secrets: set[str] = set()
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for name in _claimed_names(contribution):
            if not name.startswith("BLITZE_"):
                raise PluginError(
                    f"plugin {contribution.plugin!r} claims configuration name "
                    f"{name!r}, which must start with 'BLITZE_'"
                )
            if name in owners:
                raise PluginError(
                    f"configuration name {name!r} is claimed by both plugin "
                    f"{owners[name]!r} and plugin {contribution.plugin!r}"
                )
            owners[name] = contribution.plugin
        secrets.update(key.name for key in contribution.environment_keys)
        declared[contribution.plugin] = contribution
    unknown = sorted((set(configured) | set(from_file)) - owners.keys())
    if unknown:
        raise PluginError(
            "unknown capability configuration: "
            + ", ".join(unknown)
            + ". A name here is claimed by no installed capability: check the "
            "spelling, or whether the package that owned it was detached. In "
            "`blitzecdn.toml` the same name is written lowercase and without "
            "the prefix."
        )
    settable = sorted(set(from_file) & secrets)
    if settable:
        raise ConfigurationError(
            "these are secrets and cannot be set in blitzecdn.toml, which is "
            "committed and world-readable: "
            + ", ".join(name.removeprefix("BLITZE_").lower() for name in settable)
            + ". Set them in the controller's .env instead."
        )
    configs = {
        plugin: resolve_plugin_configuration(
            contribution, configured, from_file, state_dir
        )
        for plugin, contribution in declared.items()
    }
    # Secrets only. A setting has a resolved value whether or not an operator
    # supplied one, so forwarding the staged strings would send Ansible the
    # subset that happened to be set rather than the answer.
    environment = {key: configured[key] for key in sorted(configured) if key in secrets}
    return ResolvedCapabilityEnvironment(environment=environment, configs=configs)


def _claimed_names(contribution: ConfigurationContribution) -> tuple[str, ...]:
    """Every `BLITZE_*` name one contribution claims, secrets and settings."""
    return tuple(key.name for key in contribution.environment_keys) + tuple(
        setting.name for setting in contribution.settings
    )


def resolve_plugin_configuration(
    contribution: ConfigurationContribution,
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> CapabilityConfig:
    """Resolve one capability's own configuration, and only its own.

    Public because one caller genuinely has no control plane to ask. Backup's
    commands are built from ``Settings`` alone and open no repository — that is
    the point of them, since a controller whose database will not open is
    exactly when a restore is wanted — so ``platform.capability_config`` does
    not exist on that path. The package calls this with the contribution it
    already declared, and gets the same object the composition root would have
    handed it.

    What it deliberately does not do is the cross-plugin half: collisions with
    other capabilities and names nobody claimed are questions about the whole
    installation, and they are answered once, at startup, by
    :func:`resolve_capability_environment`. This resolves values.
    """
    values = {
        key.name: configured.get(key.name, SecretStr(""))
        for key in sorted(contribution.environment_keys, key=lambda item: item.name)
    }
    for key in contribution.environment_keys:
        _check_value(contribution.plugin, key, values[key.name])
    settings = {
        setting.name: _resolve_setting(
            contribution.plugin,
            setting,
            # The environment wins over the file, which is the precedence every
            # other source in this control plane follows.
            configured.get(setting.name)
            or _optional_secret(from_file.get(setting.name)),
            state_dir,
        )
        for setting in sorted(contribution.settings, key=lambda item: item.name)
    }
    return CapabilityConfig(contribution.plugin, values, settings)


def _optional_secret(value: str | None) -> SecretStr | None:
    """Wrap a file-sourced value so both sources reach one reader."""
    return None if value is None else SecretStr(value)


def _resolve_setting(
    plugin: str, setting: CapabilitySetting, supplied: SecretStr | None, state_dir: Path
) -> SettingValue:
    """Read one configured value as the type its capability declared.

    An unsupplied setting is its default, which is what makes a setting
    different from a secret: there is always a value, so no package has to
    carry an "or the default" expression at every read.

    A supplied one is staged as ``SecretStr`` like everything else core cannot
    yet attribute — core does not learn which names are secrets until the
    plugins declare it, and treating an unattributed value as a secret until
    told otherwise is the only safe order. Unwrapping here is the moment its
    owner has said it is not one.
    """
    if supplied is None:
        return _under(setting.default, state_dir)
    raw = supplied.get_secret_value().strip()
    if not raw:
        return _under(setting.default, state_dir)
    try:
        return _under(_coerce(setting, raw), state_dir)
    except ValueError as exc:
        raise ConfigurationError(
            f"{setting.name} is set to {raw!r}, which the capability "
            f"{plugin!r} cannot use: {exc}"
        ) from exc


#: What an operator may write for a `bool` setting. Spelled out rather than
#: passed to `bool()`, which calls every non-empty string true and would read
#: `BLITZE_SOMETHING=false` as an instruction to enable it.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _coerce(setting: CapabilitySetting, raw: str) -> SettingValue:
    """One string, as the type `setting.default` is."""
    # `bool` before `int`, because `bool` is a subclass of it.
    if isinstance(setting.default, bool):
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(
            "expected one of " + ", ".join(sorted(_TRUE | _FALSE)) + ", not a word"
        )
    if isinstance(setting.default, int):
        try:
            number = int(raw)
        except ValueError:
            raise ValueError("expected a whole number") from None
        if setting.minimum is not None and number < setting.minimum:
            raise ValueError(f"the smallest usable value is {setting.minimum}")
        if setting.maximum is not None and number > setting.maximum:
            raise ValueError(f"the largest usable value is {setting.maximum}")
        return number
    if isinstance(setting.default, Path):
        return Path(raw)
    return raw


def _under(value: SettingValue, state_dir: Path) -> SettingValue:
    """Resolve a relative path setting against this controller's state.

    Only a path is touched. A capability names a location before it can know
    where this controller keeps its state, so a relative one is read the way
    core already read its own ``backup_dir``: under the state directory. An
    absolute one is taken as written, which is what an installed controller
    configures — ``/var/backups/blitzecdn`` rather than a directory inside the
    tree an uninstall removes.
    """
    if isinstance(value, Path) and not value.is_absolute():
        return state_dir / value
    return value


def _check_value(plugin: str, key: EnvironmentKey, value: SecretStr) -> None:
    """Hold one value to the two rules core can state without reading it."""
    secret = value.get_secret_value()
    detail = f" {key.summary}" if key.summary else ""
    if not secret:
        if key.required:
            raise ConfigurationError(
                f"the installed capability {plugin!r} requires {key.name}, which "
                f"is not set on this controller.{detail}"
            )
        return
    if key.minimum_bytes and len(secret.encode("utf-8")) < key.minimum_bytes:
        raise ConfigurationError(
            f"{key.name} is set to fewer than {key.minimum_bytes} bytes, which "
            f"the capability {plugin!r} declares unusable.{detail}"
        )
