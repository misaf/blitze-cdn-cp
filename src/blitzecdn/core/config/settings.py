"""The validated settings model; where values come from is :mod:`.loading`'s job."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from blitzecdn.core.config.loading import settings_sources
from blitzecdn.core.exceptions import ConfigurationError


class Settings(BaseSettings):
    """Validated application settings, merged from the sources `loading` builds."""

    #: The fields an operator sets, as `BLITZE_<FIELD>` or a `blitzecdn.toml`
    #: key. Every other field is derived from the project directory.
    OPERATOR_SETTINGS: ClassVar[frozenset[str]] = frozenset(
        {
            "database_path",
            "environment_path",
            "deployment_timeout_seconds",
            "output_limit_bytes",
            "run_log_retention",
            "history_retention",
            "preflight_dns_timeout_seconds",
            "preflight_dns_servers",
            "allow_empty_sites",
            "origin_check_timeout_seconds",
            "drift_check_interval_seconds",
            "worker_poll_seconds",
            "api_worker_threads",
            "allowed_ips",
            "api_keys",
        }
    )
    #: Operator settings a committed file must never hold.
    ENVIRONMENT_ONLY_SETTINGS: ClassVar[frozenset[str]] = frozenset({"api_keys"})

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Constructed directly, the model reads only its arguments;
        # `from_environment` supplies the real sources.
        return (init_settings,)

    project_dir: Path
    state_dir: Path
    #: Relative to `project_dir` unless absolute, like `environment_path`.
    database_path: Path = Field(
        default=Path(".state/control-plane.db"), validate_default=True
    )
    ansible_dir: Path
    #: The inventory plugin's configuration; the hosts come from the database.
    inventory_path: Path
    playbook_path: Path
    generated_vars_path: Path
    deployment_lock_path: Path
    certificate_dir: Path
    #: Environment/secrets file. Installed controllers keep this under /etc;
    #: checkouts use the project-local `.env`. Relative to `project_dir`.
    environment_path: Path = Field(default=Path(".env"), validate_default=True)
    #: Platform teardown play. Capability-specific plays resolve from their
    #: owning modules; this setting belongs to the platform edge lifecycle.
    decommission_playbook_path: Path
    ansible_playbook: str = "ansible-playbook"
    deployment_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    #: Budget for one CAA lookup in certificate preflight; past it, the check
    #: becomes advisory rather than blocking.
    preflight_dns_timeout_seconds: int = Field(default=5, ge=1, le=60)
    #: Resolvers preflight asks instead of the host's, to see what the CA sees
    #: rather than a split-horizon view. Empty uses the host resolver.
    preflight_dns_servers: Annotated[tuple[str, ...], NoDecode] = ()
    #: `BLITZE_*` variables core does not consume, staged for features to claim
    #: through `ConfigurationContribution`. `SecretStr`, because core cannot
    #: tell which are credentials. Core's own names, the API keys included, are
    #: never staged here, so they never reach a subprocess.
    capability_environment: dict[str, SecretStr] = Field(default_factory=dict)
    #: Unknown `blitzecdn.toml` keys, staged under their `BLITZE_*` names. Kept
    #: apart from the environment so a secret can never be satisfied from the
    #: committed file: a `CapabilitySetting` reads both, an `EnvironmentKey`
    #: only the environment.
    capability_config_file: dict[str, str] = Field(default_factory=dict)
    #: The controller's own advisory origin-probe budget in certificate
    #: preflight; the fleet check's is the ``origins`` role's default.
    origin_check_timeout_seconds: int = Field(default=5, ge=1, le=60)
    #: How much of a run log to quote back in an error; the log is never cut.
    output_limit_bytes: int = Field(default=1_048_576, ge=4096, le=10_485_760)
    #: How many run logs to keep, newest first; every invocation writes one.
    run_log_retention: int = Field(default=500, ge=10, le=100_000)
    #: How many finished check-mode deployments, jobs and workflows to keep,
    #: and the baseline number of releases; referenced releases, unfinished
    #: deployments and real deployments' rollback history always survive.
    history_retention: int = Field(default=1000, ge=50, le=100_000)
    #: How many audit events to keep. A disk bound, not a retention policy: the
    #: default holds years, and a longer trail belongs off the box.
    audit_retention: int = Field(default=100_000, ge=1_000, le=10_000_000)
    drift_check_interval_seconds: int = Field(default=3600, ge=0, le=86_400)
    #: How often an idle worker polls for durable work: one indexed SELECT,
    #: rather than a broker for one convergence at a time.
    worker_poll_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    #: Size of the API's own pool for long blocking handlers, kept off the
    #: server's so a renewal sweep cannot starve `/health`. Small, because the
    #: work serialises on the deployment lock anyway.
    api_worker_threads: int = Field(default=2, ge=1, le=16)
    #: Public API client IPv4 addresses/CIDRs. Empty keeps the API on loopback.
    allowed_ips: Annotated[tuple[str, ...], NoDecode] = ()
    allow_empty_sites: bool = False
    #: `BLITZE_API_KEYS` as `operator:secret,...`, plus `BLITZE_API_KEY` as
    #: the `default` operator.
    api_keys: Annotated[dict[str, SecretStr], NoDecode] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def collect_api_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "api_key" not in data and not isinstance(data.get("api_keys"), str):
            return data
        data = dict(data)
        raw = data.get("api_keys", {})
        keys = _parse_api_keys(raw) if isinstance(raw, str) else dict(raw)
        single = data.pop("api_key", None)
        if single:
            _check_secret(single, "BLITZE_API_KEY")
            keys.setdefault("default", SecretStr(single))
        data["api_keys"] = keys
        return data

    @field_validator("preflight_dns_servers", mode="before")
    @classmethod
    def validate_dns_servers(cls, value: object) -> tuple[str, ...]:
        """Addresses only: a hostname would need the resolver we distrust."""
        servers = []
        for part in _split(value, "preflight_dns_servers"):
            try:
                ip_address(part)
            except ValueError as exc:
                raise ValueError(
                    f"preflight DNS server {part!r} is not an IP address"
                ) from exc
            servers.append(part)
        return tuple(servers)

    @field_validator("allowed_ips", mode="before")
    @classmethod
    def validate_allowed_ips(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = value.split(",") if value.strip() else []
        if not isinstance(value, (list, tuple)):
            raise ValueError("allowed_ips must be a list or comma-separated IPs/CIDRs")
        networks = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("allowed_ips entries must be non-empty IPs/CIDRs")
            entry = item.strip()
            network = ip_network(entry, strict=False)
            # The listener binds 0.0.0.0, so an IPv6 entry could admit no one.
            if network.version != 4:
                raise ValueError(
                    f"allowed_ips entry {entry!r} is IPv6; the API "
                    "listens on IPv4 and cannot admit it"
                )
            # `203.0.113.8/24` is one host or 256; masking would silently admit
            # the wider reading, so refuse and offer both.
            try:
                exact = ip_network(entry, strict=True)
            except ValueError:
                host = entry.split("/", 1)[0]
                raise ValueError(
                    f"allowed_ips entry {entry!r} has host bits set. Write "
                    f"'{network}' to admit that whole range, or '{host}/32' "
                    "to admit only that address"
                ) from None
            networks.append(str(exact))
        return tuple(dict.fromkeys(networks))

    @field_validator(
        "project_dir",
        "state_dir",
        "ansible_dir",
        "inventory_path",
        "playbook_path",
        "generated_vars_path",
        "deployment_lock_path",
        "certificate_dir",
        "decommission_playbook_path",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()

    @field_validator("database_path", "environment_path", mode="before")
    @classmethod
    def anchor_path(cls, value: object, info: ValidationInfo) -> Path:
        """Resolve a relative path against the project directory."""
        if "project_dir" not in info.data:
            raise ValueError(f"cannot resolve {info.field_name} without project_dir")
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = info.data["project_dir"] / candidate
        return candidate.expanduser().resolve()

    @property
    def run_dir(self) -> Path:
        """Working files for an invocation in flight, removed when it ends."""
        return self.state_dir / "runs"

    @property
    def log_dir(self) -> Path:
        """Raw Ansible output, one file per run, for operators; no code reads it."""
        return self.state_dir / "logs"

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        project_dir: Path | None = None,
    ) -> Self:
        sources = settings_sources(cls, environment, project_dir=project_dir)
        # `BaseSettings.__init__` by name: the field-derived signature type
        # checkers synthesise for `cls(...)` does not carry `_build_sources`.
        settings = cls.__new__(cls)
        try:
            BaseSettings.__init__(settings, _build_sources=sources)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc
        return settings

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


def _split(value: object, name: str) -> list[str]:
    """A list, or the comma-separated string an environment variable holds."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        raise ValueError(f"{name} must be a list or comma-separated")
    return [part.strip() for part in parts if part.strip()]


#: A generated key (`token_urlsafe`, hex) uses far more; fewer is a typed or
#: repeated string, which length alone would admit.
_MIN_DISTINCT_SECRET_CHARACTERS = 8


def _check_secret(secret: str, variable: str) -> None:
    """Refuse a secret too short or too uniform to have been generated."""
    if len(secret) < 32:
        raise ValueError(f"{variable} secrets must contain at least 32 characters")
    if len(set(secret)) < _MIN_DISTINCT_SECRET_CHARACTERS:
        raise ValueError(
            f"{variable} secrets must be randomly generated, for example with "
            "`openssl rand -hex 32`"
        )


def _parse_api_keys(raw: str) -> dict[str, SecretStr]:
    keys: dict[str, SecretStr] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        operator, separator, secret = entry.partition(":")
        if not separator or not operator:
            raise ValueError("BLITZE_API_KEYS entries must be operator:secret")
        _check_secret(secret, "BLITZE_API_KEYS")
        if operator in keys:
            raise ValueError(f"duplicate API operator: {operator}")
        keys[operator] = SecretStr(secret)
    return keys
