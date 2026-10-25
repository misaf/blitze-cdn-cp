"""Source precedence: where a `Settings` value comes from, as settings sources.

The model in :mod:`blitzecdn.core.config.settings` says what a setting *is*.
This module says where it comes from, as an ordered chain of pydantic-settings
sources, highest first: explicit arguments, the `BLITZE_*` environment, the
project-local `.env` (which never overrides an exported name), then
`blitzecdn.toml`, then the field default. pydantic-settings merges them;
nothing here coerces a value.

Only a field the model lists in `Settings.OPERATOR_SETTINGS` is read from a
source. Everything else is derived from the project directory by
:func:`settings_sources`.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from pydantic_settings import (
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)

from blitzecdn.assets import ansible as core_ansible
from blitzecdn.core.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from importlib.resources.abc import Traversable

    from pydantic.fields import FieldInfo
    from pydantic_settings import BaseSettings

# Disaster recovery moves portable policy and non-regenerable identity onto a
# compatible fresh install, but it must not replace the new host's filesystem
# layout with values from the failed machine.
MACHINE_SPECIFIC_CONFIG_KEYS = frozenset({"database_path", "environment_path"})
MACHINE_SPECIFIC_ENVIRONMENT_KEYS = frozenset(
    {
        "BLITZE_PROJECT_DIR",
        "BLITZE_CONFIG",
        "BLITZE_DATABASE_PATH",
        "BLITZE_ENVIRONMENT_PATH",
    }
)

_PREFIX = "BLITZE_"
#: Variables spelled other than `BLITZE_` + the field name, mapped to the name
#: they stand for. The natural spelling is not accepted in their place.
_RENAMED_VARIABLES = {
    "BLITZE_DEPLOYMENT_OUTPUT_LIMIT_BYTES": "BLITZE_OUTPUT_LIMIT_BYTES",
}
#: `BLITZE_*` names core reads that are not a field of their own. The single
#: API key is folded into `api_keys` by the model.
_BOOTSTRAP_VARIABLES = frozenset({"BLITZE_PROJECT_DIR", "BLITZE_CONFIG"})
_API_KEY_VARIABLE = "BLITZE_API_KEY"


def is_portable_config_key(name: str) -> bool:
    """Whether this `blitzecdn.toml` key survives a restore onto a fresh host.

    A predicate rather than an allow-list: a capability owns its own keys in
    this file and core must not name them. A capability's own machine-specific
    setting is declared on the setting, as `CapabilitySetting.portable`.
    """
    return name not in MACHINE_SPECIFIC_CONFIG_KEYS


def is_portable_environment_key(name: str) -> bool:
    """Whether this environment setting survives a restore onto a fresh host.

    Everything but the handful of settings that describe *this machine* is
    portable, so core never has to name a capability's variable to say so.
    """
    return name.startswith(_PREFIX) and name not in MACHINE_SPECIFIC_ENVIRONMENT_KEYS


def _operator_settings(
    settings_cls: type[BaseSettings], *, environment: bool
) -> frozenset[str]:
    """The fields a source may set: every operator setting, or the file's."""
    fields: frozenset[str] = getattr(settings_cls, "OPERATOR_SETTINGS", frozenset())
    if environment:
        return fields
    secret: frozenset[str] = getattr(
        settings_cls, "ENVIRONMENT_ONLY_SETTINGS", frozenset()
    )
    return fields - secret


class EnvironmentSource(EnvSettingsSource):
    """`BLITZE_*` settings from a given mapping, never from `os.environ`.

    Names match exactly and in upper case, the way a shell spells them. Every
    other `BLITZE_*` name is staged, masked, in `capability_environment` for the
    feature resolver to claim: core cannot tell a credential from a tuning
    value, and the safe assumption for one it cannot interpret is that it is a
    credential. Core's own names, the API keys among them, are never staged, so
    they never reach a subprocess.
    """

    def __init__(
        self, settings_cls: type[BaseSettings], environment: Mapping[str, str]
    ) -> None:
        self._environment = dict(environment)
        super().__init__(settings_cls, case_sensitive=False, env_prefix=_PREFIX)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        found: dict[str, str | None] = {}
        for name, value in self._environment.items():
            if not name.startswith(_PREFIX) or name != name.upper():
                continue
            if name in _RENAMED_VARIABLES.values():
                continue
            found[_RENAMED_VARIABLES.get(name, name).lower()] = value
        return found

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if field_name not in _operator_settings(self.settings_cls, environment=True):
            return None, field_name, False
        return super().get_field_value(field, field_name)

    def _core_variables(self) -> frozenset[str]:
        natural = {
            _PREFIX + name.upper()
            for name in _operator_settings(self.settings_cls, environment=True)
        }
        return frozenset(
            (natural - set(_RENAMED_VARIABLES.values()))
            | set(_RENAMED_VARIABLES)
            | _BOOTSTRAP_VARIABLES
            | {_API_KEY_VARIABLE}
        )

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        core = self._core_variables()
        staged = {
            name: SecretStr(value)
            for name, value in sorted(self._environment.items())
            if name.startswith(_PREFIX) and name not in core
        }
        if staged:
            data["capability_environment"] = staged
        if _API_KEY_VARIABLE in self._environment:
            data["api_key"] = self._environment[_API_KEY_VARIABLE]
        return data


class ProjectFileSource(TomlConfigSettingsSource):
    """The `[blitzecdn]` table of `blitzecdn.toml`.

    A key core does not recognise is staged in `capability_config_file` under
    the `BLITZE_*` name it corresponds to, rather than refused: this runs long
    before any feature has said what it claims. Kept apart from the
    environment's staging, and never `SecretStr`, because this file is
    committed: only a `CapabilitySetting` is ever satisfied from it, never a
    secret declared as an `EnvironmentKey`.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        self._known = _operator_settings(settings_cls, environment=False)
        self._staged: dict[str, str] = {}
        super().__init__(settings_cls, toml_file=path)

    def _read_file(self, file_path: Path | Traversable) -> dict[str, Any]:
        try:
            with file_path.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"invalid TOML in {file_path}: {exc}") from exc
        table = document.get("blitzecdn", {})
        if not isinstance(table, dict):
            raise ConfigurationError("[blitzecdn] must be a TOML table")
        self._staged = {
            _PREFIX + str(name).upper(): _scalar(value)
            for name, value in sorted(table.items())
            if name not in self._known
        }
        return {name: value for name, value in table.items() if name in self._known}

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        if self._staged:
            data["capability_config_file"] = self._staged
        return data


def settings_sources(
    settings_cls: type[BaseSettings],
    environment: Mapping[str, str] | None = None,
    *,
    project_dir: Path | None = None,
) -> tuple[tuple[PydanticBaseSettingsSource, ...], dict[str, Any]]:
    """Every source for one `Settings`, highest precedence first.

    Shaped for `BaseSettings(_build_sources=...)`. The project root is resolved
    twice, deliberately: `.env` is read relative to the root the process
    already knows, and only then may that file move the root by setting
    `BLITZE_PROJECT_DIR` itself. An explicit `project_dir` outranks both.
    """
    supplied = os.environ if environment is None else environment
    initial_root = (
        project_dir or Path(supplied.get("BLITZE_PROJECT_DIR", Path.cwd()))
    ).resolve()
    local = read_environment_file(initial_root / ".env")
    merged = {**local, **supplied}
    root = (
        project_dir or Path(merged.get("BLITZE_PROJECT_DIR", initial_root))
    ).resolve()
    config = Path(merged.get("BLITZE_CONFIG", root / "blitzecdn.toml"))
    layout = _layout(root)
    return (
        (
            InitSettingsSource(settings_cls, layout),
            EnvironmentSource(settings_cls, supplied),
            EnvironmentSource(settings_cls, local),
            ProjectFileSource(settings_cls, config),
        ),
        layout,
    )


def _layout(root: Path) -> dict[str, Any]:
    """The paths derived from the project root, which no source may set."""
    state = root / ".state"
    return {
        "project_dir": root,
        "state_dir": state,
        # Not derived from `root`. The platform's Ansible ships inside this
        # wheel and is located through `importlib.resources`, so these resolve
        # identically in a checkout and on a controller that has no checkout
        # at all. Pointing the control plane at someone else's copy of the
        # platform roles is not a supported deployment.
        "ansible_dir": core_ansible.ROLES_PATH.parent,
        "inventory_path": core_ansible.INVENTORY_PATH,
        "playbook_path": core_ansible.EDGE_PLAYBOOK,
        "decommission_playbook_path": core_ansible.DECOMMISSION_PLAYBOOK,
        "generated_vars_path": state / "desired-state.yml",
        "deployment_lock_path": state / "deployment.lock",
        "certificate_dir": state / "certificates",
    }


def read_environment_file(path: Path) -> dict[str, str]:
    """Read a `.env` strictly: one shell-quoted `NAME=value` per line.

    Stricter than python-dotenv on purpose. A symlink or a value with an
    unquoted space is refused rather than guessed at, because this file holds
    the controller's secrets.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"refusing to load unsafe environment file: {path}")
    for number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not name or not name.replace("_", "a").isalnum():
            raise ConfigurationError(f"invalid assignment in {path}:{number}")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigurationError(
                f"invalid value in {path}:{number}: {exc}"
            ) from exc
        if len(parts) > 1:
            raise ConfigurationError(
                f"quote values containing spaces in {path}:{number}"
            )
        values.setdefault(name, parts[0] if parts else "")
    return values


def _scalar(value: object) -> str:
    """One TOML value as the string an environment variable would have held.

    `True` is the one that would otherwise be wrong: Python spells it
    capitalised and no environment variable ever does.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        raise ConfigurationError(
            f"capability configuration must be a single value, not a "
            f"{type(value).__name__}"
        )
    return str(value)
