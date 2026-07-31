from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from blitzecdn.exceptions import ConfigurationError

_PROJECT_KEYS = {
    "state_dir",
    "database_path",
    "ansible_dir",
    "inventory_path",
    "playbook_path",
    "generated_vars_path",
    "deployment_lock_path",
    "ansible_playbook",
    "deployment_timeout_seconds",
    "output_limit_bytes",
}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_dir: Path
    state_dir: Path
    database_path: Path
    ansible_dir: Path
    inventory_path: Path
    playbook_path: Path
    generated_vars_path: Path
    deployment_lock_path: Path
    ansible_playbook: str = "ansible-playbook"
    deployment_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    output_limit_bytes: int = Field(default=1_048_576, ge=4096, le=10_485_760)
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator(
        "project_dir",
        "state_dir",
        "database_path",
        "ansible_dir",
        "inventory_path",
        "playbook_path",
        "generated_vars_path",
        "deployment_lock_path",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        project_dir: Path | None = None,
    ) -> Self:
        env = os.environ if environment is None else environment
        root = (
            project_dir or Path(env.get("BLITZE_PROJECT_DIR", Path.cwd()))
        ).resolve()
        project_config = cls._read_project_config(
            Path(env.get("BLITZE_CONFIG", root / "blitzecdn.toml"))
        )

        def value(environment_name: str, config_name: str, default: object) -> object:
            return env.get(environment_name, project_config.get(config_name, default))

        def path_value(environment_name: str, config_name: str, default: Path) -> Path:
            candidate = Path(str(value(environment_name, config_name, default)))
            return candidate if candidate.is_absolute() else root / candidate

        state = path_value("BLITZE_STATE_DIR", "state_dir", root / ".state")
        keys = cls._read_api_keys(env)
        try:
            return cls(
                project_dir=root,
                state_dir=state,
                database_path=path_value(
                    "BLITZE_DATABASE_PATH",
                    "database_path",
                    state / "control-plane.db",
                ),
                ansible_dir=path_value(
                    "BLITZE_ANSIBLE_DIR", "ansible_dir", root / "ansible"
                ),
                inventory_path=path_value(
                    "BLITZE_INVENTORY",
                    "inventory_path",
                    root / "ansible/inventory/hosts.yml",
                ),
                playbook_path=path_value(
                    "BLITZE_PLAYBOOK",
                    "playbook_path",
                    root / "ansible/playbooks/edge.yml",
                ),
                generated_vars_path=path_value(
                    "BLITZE_GENERATED_VARS",
                    "generated_vars_path",
                    state / "desired-state.yml",
                ),
                deployment_lock_path=path_value(
                    "BLITZE_DEPLOYMENT_LOCK",
                    "deployment_lock_path",
                    state / "deployment.lock",
                ),
                ansible_playbook=str(
                    value(
                        "BLITZE_ANSIBLE_PLAYBOOK",
                        "ansible_playbook",
                        "ansible-playbook",
                    )
                ),
                deployment_timeout_seconds=int(
                    str(
                        value(
                            "BLITZE_DEPLOYMENT_TIMEOUT_SECONDS",
                            "deployment_timeout_seconds",
                            900,
                        )
                    )
                ),
                output_limit_bytes=int(
                    str(
                        value(
                            "BLITZE_DEPLOYMENT_OUTPUT_LIMIT_BYTES",
                            "output_limit_bytes",
                            1_048_576,
                        )
                    )
                ),
                api_keys=keys,
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc

    @staticmethod
    def _read_project_config(path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc
        raw = document.get("blitzecdn", {})
        if not isinstance(raw, dict):
            raise ConfigurationError("[blitzecdn] must be a TOML table")
        unknown = set(raw) - _PROJECT_KEYS
        if unknown:
            raise ConfigurationError(
                f"unknown project configuration: {', '.join(sorted(unknown))}"
            )
        return raw

    @staticmethod
    def _read_api_keys(env: Mapping[str, str]) -> dict[str, SecretStr]:
        keys: dict[str, SecretStr] = {}
        raw_keys = env.get("BLITZE_API_KEYS", "")
        for entry in filter(None, (part.strip() for part in raw_keys.split(","))):
            operator, separator, secret = entry.partition(":")
            if not separator or not operator or len(secret) < 32:
                raise ConfigurationError(
                    "BLITZE_API_KEYS entries must be operator:secret with secrets "
                    "of at least 32 characters"
                )
            if operator in keys:
                raise ConfigurationError(f"duplicate API operator: {operator}")
            keys[operator] = SecretStr(secret)
        single = env.get("BLITZE_API_KEY")
        if single:
            if len(single) < 32:
                raise ConfigurationError(
                    "BLITZE_API_KEY must contain at least 32 characters"
                )
            keys.setdefault("default", SecretStr(single))
        return keys

    def validate_runtime(self, *, require_auth: bool = False) -> list[str]:
        errors: list[str] = []
        if require_auth and not self.api_keys:
            errors.append("no API keys configured")
        for label, path in (
            ("Ansible directory", self.ansible_dir),
            ("inventory", self.inventory_path),
            ("playbook", self.playbook_path),
        ):
            if not path.exists():
                errors.append(f"{label} does not exist: {path}")
        if (
            self.generated_vars_path.parent == self.ansible_dir
            or self.generated_vars_path.parent in self.ansible_dir.parents
        ):
            errors.append(
                "generated vars must be a file under the state directory, "
                "not the Ansible source directory"
            )
        return errors
