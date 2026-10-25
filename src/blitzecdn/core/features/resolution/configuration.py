"""What an operator configured, resolved into what each capability may read.

Unlike the other resolvers, the result is handed back scoped: a capability
reads the keys it declared and cannot see another's credential.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, FeatureError
from blitzecdn.core.features.types import (
    CapabilitySetting,
    ConfigurationContribution,
    EnvironmentKey,
    SettingValue,
)

__all__ = [
    "CapabilityConfig",
    "ResolvedCapabilityEnvironment",
    "resolve_capability_environment",
    "resolve_feature_configuration",
]


@dataclass(frozen=True, slots=True)
class CapabilityConfig:
    """One feature's own configuration, and nothing else's.

    An unset declared key reads as an empty secret; an undeclared one is refused,
    because at the call site that is a typo, not an absence.
    """

    feature: str
    values: Mapping[str, SecretStr]
    #: The non-secret half, coerced to each setting's declared type. A setting
    #: always has a value; a secret is only ever asked whether it is set.
    settings: Mapping[str, SettingValue] = field(default_factory=dict)

    def secret(self, name: str) -> SecretStr:
        """This key's value, empty when the controller has not set it."""
        if name not in self.values:
            raise FeatureError(
                f"feature {self.feature!r} reads the environment key {name!r}, "
                "which it does not declare. A secret is claimed once, in its "
                "`ConfigurationContribution.environment_keys`, and read by "
                "that name."
            )
        return self.values[name]

    def is_set(self, name: str) -> bool:
        """Whether the controller supplied a value for this declared key."""
        return bool(self.secret(name).get_secret_value())

    def _setting(self, name: str, kind: type) -> SettingValue:
        """One declared setting, checked against the type the call site expects.

        Catches a setting later redeclared as another type with a clear message
        rather than a `TypeError` several frames away.
        """
        if name not in self.settings:
            raise FeatureError(
                f"feature {self.feature!r} reads the setting {name!r}, which it "
                "does not declare. A setting is claimed once, in its "
                "`ConfigurationContribution.settings`, and read by that name."
            )
        value = self.settings[name]
        # `bool` before `int`, as everywhere this type is inspected.
        matches = (
            isinstance(value, bool) if kind is bool else isinstance(value, kind)
        ) and not (kind is int and isinstance(value, bool))
        if not matches:
            raise FeatureError(
                f"feature {self.feature!r} reads {name!r} as {kind.__name__}, "
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
    """Every claimed secret, flat for Ansible and scoped per feature.

    ``environment`` is what ``PlaybookExecutor`` copies into the subprocess, where
    roles read names with ``lookup('env', ...)``. ``configs`` is what a capability
    reads on the controller.
    """

    environment: Mapping[str, SecretStr]
    configs: Mapping[str, CapabilityConfig]

    def for_feature(self, feature: str) -> CapabilityConfig:
        """This feature's configuration; empty, not an error, if it declared no keys."""
        return self.configs.get(feature) or CapabilityConfig(feature, {})


def resolve_capability_environment(
    contributions: Iterable[ConfigurationContribution],
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> ResolvedCapabilityEnvironment:
    """Resolve claimed configuration, and refuse what cannot be worked with.

    Refuses an unprefixed claim, a name claimed twice and a configured name nobody
    claims (``FeatureError``), and an unusable value (``ConfigurationError``).
    ``configured`` is the environment and the 0600 ``.env``; ``from_file`` is
    ``blitzecdn.toml``. A setting may come from either, the environment winning; a
    secret only from the first. Only secrets are forwarded to Ansible.
    """
    owners: dict[str, str] = {}
    declared: dict[str, ConfigurationContribution] = {}
    secrets: set[str] = set()
    for contribution in sorted(contributions, key=lambda item: item.feature):
        for name in _claimed_names(contribution):
            if not name.startswith("BLITZE_"):
                raise FeatureError(
                    f"feature {contribution.feature!r} claims configuration name "
                    f"{name!r}, which must start with 'BLITZE_'"
                )
            if name in owners:
                raise FeatureError(
                    f"configuration name {name!r} is claimed by both feature "
                    f"{owners[name]!r} and feature {contribution.feature!r}"
                )
            owners[name] = contribution.feature
        secrets.update(key.name for key in contribution.environment_keys)
        declared[contribution.feature] = contribution
    unknown = sorted((set(configured) | set(from_file)) - owners.keys())
    if unknown:
        raise FeatureError(
            "unknown capability configuration: "
            + ", ".join(unknown)
            + ". A name here is claimed by no capability: check the spelling, "
            "or whether the capability that owned it was removed. In "
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
        feature: resolve_feature_configuration(
            contribution, configured, from_file, state_dir
        )
        for feature, contribution in declared.items()
    }
    # Secrets only: forwarding staged settings would send the subset that
    # happened to be set rather than the resolved values.
    environment = {key: configured[key] for key in sorted(configured) if key in secrets}
    return ResolvedCapabilityEnvironment(environment=environment, configs=configs)


def _claimed_names(contribution: ConfigurationContribution) -> tuple[str, ...]:
    """Every `BLITZE_*` name one contribution claims, secrets and settings."""
    return tuple(key.name for key in contribution.environment_keys) + tuple(
        setting.name for setting in contribution.settings
    )


def resolve_feature_configuration(
    contribution: ConfigurationContribution,
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> CapabilityConfig:
    """Resolve one capability's own configuration, without the cross-feature checks.

    For callers with no control plane — backup's commands must work when the
    database will not open. Collisions and unclaimed names are checked once at
    startup by :func:`resolve_capability_environment`.
    """
    values = {
        key.name: configured.get(key.name, SecretStr(""))
        for key in sorted(contribution.environment_keys, key=lambda item: item.name)
    }
    for key in contribution.environment_keys:
        _check_value(contribution.feature, key, values[key.name])
    settings = {
        setting.name: _resolve_setting(
            contribution.feature,
            setting,
            # The environment wins over the file, which is the precedence every
            # other source in this control plane follows.
            configured.get(setting.name)
            or _optional_secret(from_file.get(setting.name)),
            state_dir,
        )
        for setting in sorted(contribution.settings, key=lambda item: item.name)
    }
    return CapabilityConfig(contribution.feature, values, settings)


def _optional_secret(value: str | None) -> SecretStr | None:
    """Wrap a file-sourced value so both sources reach one reader."""
    return None if value is None else SecretStr(value)


def _resolve_setting(
    feature: str,
    setting: CapabilitySetting,
    supplied: SecretStr | None,
    state_dir: Path,
) -> SettingValue:
    """Read one configured value as the type its capability declared.

    An unsupplied setting is its default. A supplied one arrives as ``SecretStr``,
    because core stages every unattributed value as secret until its owner says
    otherwise; this is where it is unwrapped.
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
            f"{feature!r} cannot use: {exc}"
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
    """Resolve a relative path setting under the state directory; keep absolute ones."""
    if isinstance(value, Path) and not value.is_absolute():
        return state_dir / value
    return value


def _check_value(feature: str, key: EnvironmentKey, value: SecretStr) -> None:
    """Hold one value to the two rules core can state without reading it."""
    secret = value.get_secret_value()
    detail = f" {key.summary}" if key.summary else ""
    if not secret:
        if key.required:
            raise ConfigurationError(
                f"the installed capability {feature!r} requires {key.name}, which "
                f"is not set on this controller.{detail}"
            )
        return
    if key.minimum_bytes and len(secret.encode("utf-8")) < key.minimum_bytes:
        raise ConfigurationError(
            f"{key.name} is set to fewer than {key.minimum_bytes} bytes, which "
            f"the capability {feature!r} declares unusable.{detail}"
        )
