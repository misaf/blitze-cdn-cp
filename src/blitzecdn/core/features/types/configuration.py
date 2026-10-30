"""What a capability asks an operator to configure.

Secrets core forwards without reading, settings core resolves and hands back
typed, and the contribution that carries both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, SecretStr

__all__ = [
    "CapabilitySetting",
    "ConfigurationContribution",
    "ConfigurationModel",
    "EnvironmentKey",
    "SettingValue",
]


class ConfigurationModel(BaseModel):
    """A capability's typed settings, also the source of its public declarations.

    Fields use their existing ``BLITZE_*`` name as an alias. Pydantic owns
    defaults, validation and normalization; declarations remain available to
    introspection and backup portability checks.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def configuration(cls, feature: str) -> ConfigurationContribution:
        defaults = cls()
        secrets: list[EnvironmentKey] = []
        settings: list[CapabilitySetting] = []
        for name, field in cls.model_fields.items():
            alias = field.alias
            if not alias or not alias.startswith("BLITZE_"):
                raise ValueError(f"{cls.__name__}.{name} needs a BLITZE_* alias")
            value = getattr(defaults, name)
            metadata = field.json_schema_extra or {}
            if not isinstance(metadata, dict):
                raise ValueError(f"{cls.__name__}.{name} needs dictionary metadata")
            if isinstance(value, SecretStr):
                secrets.append(
                    EnvironmentKey(
                        name=alias,
                        summary=field.description or "",
                        minimum_bytes=int(cast(int, metadata.get("minimum_bytes", 0))),
                    )
                )
            else:
                bounds = {
                    key: getattr(item, key)
                    for item in field.metadata
                    for key in ("ge", "le")
                    if getattr(item, key, None) is not None
                }
                settings.append(
                    CapabilitySetting(
                        name=alias,
                        default="" if value is None else value,
                        summary=field.description or "",
                        minimum=bounds.get("ge"),
                        maximum=bounds.get("le"),
                        portable=metadata.get("portable", True) is not False,
                    )
                )
        return ConfigurationContribution(
            feature=feature,
            environment_keys=tuple(secrets),
            settings=tuple(settings),
            model=cls,
        )


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    """One secret `BLITZE_*` name a capability claims, and its shape.

    Core never reads the value; presence and length are checked at composition,
    before any adapter exists. `required` is for a key whose absence makes the
    feature wrong rather than idle. `summary` is shown when the key is missing
    and by ``blitzecdn features``: say where to get the value.
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


#: What a non-secret setting may be: each type has one unambiguous spelling as
#: an environment variable or TOML scalar. Anything richer is a structure, and
#: belongs in the capability's own file rather than a `BLITZE_*` name.
type SettingValue = str | int | bool | Path


@dataclass(frozen=True, slots=True)
class CapabilitySetting:
    """One non-secret `BLITZE_*` name a capability claims.

    `default` carries the type as well as the value. A relative `Path` default
    resolves against the controller's state directory. `minimum` and `maximum`
    apply to an ``int`` only. `summary` is shown by ``blitzecdn features``: say
    what changing the value does.
    """

    name: str
    default: SettingValue
    summary: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: Whether this setting survives a restore onto a different host. False only
    #: for a value describing the machine, such as ``backup``'s archive
    #: directory, which would otherwise point a rebuilt controller at a path
    #: belonging to the host that died.
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
    """Everything one capability asks an operator to configure.

    Both lists are claims: core refuses a configured ``BLITZE_*`` name that no
    capability claims.
    """

    feature: str
    #: The owning Pydantic model validates every configuration contribution.
    model: type[ConfigurationModel]
    #: Secrets. Only these are copied into Ansible's subprocess environment,
    #: they remain ``SecretStr`` throughout, and they never travel through argv
    #: or desired state.
    environment_keys: tuple[EnvironmentKey, ...] = ()
    #: Non-secrets. Resolved to the declared type and handed back through
    #: :class:`~blitzecdn.core.features.resolution.CapabilityConfig`.
    settings: tuple[CapabilitySetting, ...] = ()
