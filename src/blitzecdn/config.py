from __future__ import annotations

import os
import shlex
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
    "certificate_dir",
    "acme_challenge_playbook_path",
    "cache_purge_playbook_path",
    "stats_playbook_path",
    "decommission_playbook_path",
    "certbot",
    "acme_default_email",
    "acme_ca_domain",
    "origin_check_timeout_seconds",
    "preflight_dns_timeout_seconds",
    "certificate_reconcile_interval_seconds",
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
    certificate_dir: Path
    acme_challenge_playbook_path: Path
    cache_purge_playbook_path: Path
    stats_playbook_path: Path
    decommission_playbook_path: Path
    ansible_playbook: str = "ansible-playbook"
    certbot: str = "certbot"
    acme_default_email: str | None = None
    #: The CA identity a CAA record has to name for our issuance to be allowed.
    #: Configurable because `certbot` can be pointed at another ACME server, and
    #: a CAA check against the wrong CA is worse than none — it would pass while
    #: the real CA is refused.
    acme_ca_domain: str = "letsencrypt.org"
    deployment_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    #: Budget for one CAA lookup during certificate preflight. A resolver that
    #: needs longer is treated as unavailable and the check downgrades to an
    #: advisory rather than blocking issuance.
    preflight_dns_timeout_seconds: int = Field(default=5, ge=1, le=60)
    #: Per-origin budget for `blitzecdn origin check`. Short on purpose: an
    #: origin that needs longer than this to answer a bare TCP connect is one
    #: the edges will struggle with too.
    origin_check_timeout_seconds: int = Field(default=5, ge=1, le=60)
    output_limit_bytes: int = Field(default=1_048_576, ge=4096, le=10_485_760)
    certificate_reconcile_interval_seconds: int = Field(default=600, ge=0, le=86_400)
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
        "certificate_dir",
        "acme_challenge_playbook_path",
        "cache_purge_playbook_path",
        "stats_playbook_path",
        "decommission_playbook_path",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()

    @field_validator("acme_ca_domain")
    @classmethod
    def validate_acme_ca_domain(cls, value: str) -> str:
        """Reject an empty CA identity rather than blocking every issuance.

        The CAA check asks whether this name appears in the record's allowed
        issuers. Empty never appears, so an unset value would turn a passing
        preflight into a permanent, unexplained refusal.
        """
        candidate = value.strip().lower().rstrip(".")
        if (
            not candidate
            or "." not in candidate
            or any(character.isspace() for character in candidate)
        ):
            raise ValueError(
                "acme_ca_domain must be a CA identity such as 'letsencrypt.org'"
            )
        return candidate

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        project_dir: Path | None = None,
    ) -> Self:
        supplied = os.environ if environment is None else environment
        initial_root = (
            project_dir or Path(supplied.get("BLITZE_PROJECT_DIR", Path.cwd()))
        ).resolve()
        env = cls._with_local_environment(supplied, initial_root)
        root = (
            project_dir or Path(env.get("BLITZE_PROJECT_DIR", initial_root))
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
                certificate_dir=path_value(
                    "BLITZE_CERTIFICATE_DIR",
                    "certificate_dir",
                    state / "certificates",
                ),
                acme_challenge_playbook_path=path_value(
                    "BLITZE_ACME_CHALLENGE_PLAYBOOK",
                    "acme_challenge_playbook_path",
                    root / "ansible/playbooks/acme-challenge.yml",
                ),
                cache_purge_playbook_path=path_value(
                    "BLITZE_CACHE_PURGE_PLAYBOOK",
                    "cache_purge_playbook_path",
                    root / "ansible/playbooks/cache-purge.yml",
                ),
                stats_playbook_path=path_value(
                    "BLITZE_STATS_PLAYBOOK",
                    "stats_playbook_path",
                    root / "ansible/playbooks/stats.yml",
                ),
                decommission_playbook_path=path_value(
                    "BLITZE_DECOMMISSION_PLAYBOOK",
                    "decommission_playbook_path",
                    root / "ansible/playbooks/decommission.yml",
                ),
                ansible_playbook=str(
                    value(
                        "BLITZE_ANSIBLE_PLAYBOOK",
                        "ansible_playbook",
                        "ansible-playbook",
                    )
                ),
                certbot=str(value("BLITZE_CERTBOT", "certbot", "certbot")),
                acme_default_email=(
                    str(raw_email).strip().lower()
                    if (
                        raw_email := value(
                            "BLITZE_ACME_DEFAULT_EMAIL", "acme_default_email", ""
                        )
                    )
                    else None
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
                acme_ca_domain=str(
                    value("BLITZE_ACME_CA_DOMAIN", "acme_ca_domain", "letsencrypt.org")
                )
                .strip()
                .lower()
                .rstrip("."),
                preflight_dns_timeout_seconds=int(
                    str(
                        value(
                            "BLITZE_PREFLIGHT_DNS_TIMEOUT_SECONDS",
                            "preflight_dns_timeout_seconds",
                            5,
                        )
                    )
                ),
                origin_check_timeout_seconds=int(
                    str(
                        value(
                            "BLITZE_ORIGIN_CHECK_TIMEOUT_SECONDS",
                            "origin_check_timeout_seconds",
                            5,
                        )
                    )
                ),
                certificate_reconcile_interval_seconds=int(
                    str(
                        value(
                            "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS",
                            "certificate_reconcile_interval_seconds",
                            600,
                        )
                    )
                ),
                api_keys=keys,
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc

    @staticmethod
    def _with_local_environment(
        environment: Mapping[str, str], project_dir: Path
    ) -> dict[str, str]:
        """Load project-local defaults without overriding the process environment."""
        merged = dict(environment)
        path = project_dir / ".env"
        if not path.exists():
            return merged
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(
                f"refusing to load unsafe environment file: {path}"
            )
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
            merged.setdefault(name, parts[0] if parts else "")
        return merged

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
        if not self.generated_vars_path.is_relative_to(self.state_dir):
            errors.append(
                "generated vars must be a file under the state directory, "
                "not the Ansible source directory"
            )
        return errors
