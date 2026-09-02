from __future__ import annotations

import ipaddress
import os
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import Field, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from blitzecdn.core.exceptions import ConfigurationError

_PATH_SETTINGS = (
    ("environment_path", "BLITZE_ENVIRONMENT_PATH", "environment_path", ".env"),
)

_STATE_PATH_SETTINGS = (
    ("database_path", "BLITZE_DATABASE_PATH", "database_path", "control-plane.db"),
    # Under the state directory for a checkout, and repointed at
    # `/var/backups/blitzecdn` by the managed configuration a real installation
    # gets. Both are right for where they are: a developer should not need a
    # privileged directory to take a backup, and an installed controller should
    # not keep its backups inside the directory an uninstall removes.
    ("backup_dir", "BLITZE_BACKUP_DIR", "backup_dir", "backups"),
)

_VALUE_SETTINGS: tuple[tuple[str, str, str, object], ...] = (
    (
        "deployment_timeout_seconds",
        "BLITZE_DEPLOYMENT_TIMEOUT_SECONDS",
        "deployment_timeout_seconds",
        900,
    ),
    (
        "output_limit_bytes",
        "BLITZE_DEPLOYMENT_OUTPUT_LIMIT_BYTES",
        "output_limit_bytes",
        1_048_576,
    ),
    ("run_log_retention", "BLITZE_RUN_LOG_RETENTION", "run_log_retention", 500),
    ("history_retention", "BLITZE_HISTORY_RETENTION", "history_retention", 1000),
    (
        "preflight_dns_timeout_seconds",
        "BLITZE_PREFLIGHT_DNS_TIMEOUT_SECONDS",
        "preflight_dns_timeout_seconds",
        5,
    ),
    ("allow_empty_sites", "BLITZE_ALLOW_EMPTY_SITES", "allow_empty_sites", False),
    (
        "origin_check_timeout_seconds",
        "BLITZE_ORIGIN_CHECK_TIMEOUT_SECONDS",
        "origin_check_timeout_seconds",
        5,
    ),
    (
        "certificate_reconcile_interval_seconds",
        "BLITZE_CERTIFICATE_RECONCILE_INTERVAL_SECONDS",
        "certificate_reconcile_interval_seconds",
        600,
    ),
    (
        "certificate_renewal_interval_seconds",
        "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS",
        "certificate_renewal_interval_seconds",
        43_200,
    ),
    (
        "ssl_automatic_scan_interval_seconds",
        "BLITZE_SSL_AUTOMATIC_SCAN_INTERVAL_SECONDS",
        "ssl_automatic_scan_interval_seconds",
        2_592_000,
    ),
    (
        "drift_check_interval_seconds",
        "BLITZE_DRIFT_CHECK_INTERVAL_SECONDS",
        "drift_check_interval_seconds",
        3600,
    ),
    ("redis_url", "BLITZE_REDIS_URL", "redis_url", "redis://127.0.0.1:6379/0"),
    (
        "certificate_renewal_budget_seconds",
        "BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS",
        "certificate_renewal_budget_seconds",
        300,
    ),
    (
        "certificate_renewal_workers",
        "BLITZE_CERTIFICATE_RENEWAL_WORKERS",
        "certificate_renewal_workers",
        2,
    ),
)

_PROJECT_KEYS = {
    *(spec[2] for spec in (*_PATH_SETTINGS, *_STATE_PATH_SETTINGS, *_VALUE_SETTINGS)),
    "acme_default_email",
    "preflight_dns_servers",
    "required_capabilities",
}

# Disaster recovery moves portable policy and non-regenerable identity onto a
# compatible fresh install, but it must not replace the new host's filesystem
# layout or local Redis endpoint with values from the failed machine.
MACHINE_SPECIFIC_CONFIG_KEYS = frozenset(
    {"database_path", "backup_dir", "environment_path", "redis_url"}
)
PORTABLE_CONFIG_KEYS = frozenset(_PROJECT_KEYS - MACHINE_SPECIFIC_CONFIG_KEYS)
MACHINE_SPECIFIC_ENVIRONMENT_KEYS = frozenset(
    {
        "BLITZE_PROJECT_DIR",
        "BLITZE_CONFIG",
        "BLITZE_DATABASE_PATH",
        "BLITZE_BACKUP_DIR",
        "BLITZE_ENVIRONMENT_PATH",
        "BLITZE_REDIS_URL",
    }
)
#: Every `BLITZE_*` name core reads for itself. A name here can never be claimed
#: by an optional package or forwarded as capability configuration.
_CORE_ENVIRONMENT_KEYS = frozenset(
    {
        *(
            spec[1]
            for spec in (*_PATH_SETTINGS, *_STATE_PATH_SETTINGS, *_VALUE_SETTINGS)
        ),
        "BLITZE_PROJECT_DIR",
        "BLITZE_CONFIG",
        "BLITZE_ACME_DEFAULT_EMAIL",
        "BLITZE_PREFLIGHT_DNS_SERVERS",
        "BLITZE_REQUIRED_CAPABILITIES",
        # Controller authentication. Deliberately core-only and deliberately
        # never forwarded: no edge has any business holding the key that
        # authenticates to the control plane's own API.
        "BLITZE_API_KEY",
        "BLITZE_API_KEYS",
    }
)


def is_portable_environment_key(name: str) -> bool:
    """Whether this environment setting survives a restore onto a fresh host.

    A predicate rather than the allow-list it replaces, because the list can no
    longer be written down: an optional capability owns its own `BLITZE_*`
    names, and a controller may have capabilities installed that this
    repository has never heard of. Naming `BLITZE_MAXMIND_LICENSE_KEY` here to
    keep it portable would put a capability's name back in core to answer a
    question core can answer generically — everything but the handful of
    settings that describe *this machine* is portable.
    """
    return name.startswith("BLITZE_") and name not in MACHINE_SPECIFIC_ENVIRONMENT_KEYS


class Settings(BaseSettings):
    """Validated application settings assembled from environment and TOML.

    ``from_environment`` owns source precedence and the path defaults that are
    relative to ``project_dir``.  ``BaseSettings`` owns typed coercion and the
    settings model itself, so booleans, integers and secrets no longer need a
    parallel hand-written conversion layer.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    project_dir: Path
    state_dir: Path
    database_path: Path
    ansible_dir: Path
    #: The Ansible inventory *source*, which is the `blitzecdn` plugin's
    #: configuration rather than a list of hosts. The fleet itself comes from
    #: the `edges` table; this file only tells Ansible to go and read it.
    inventory_path: Path
    playbook_path: Path
    generated_vars_path: Path
    deployment_lock_path: Path
    certificate_dir: Path
    #: Environment/secrets file. Installed controllers keep this under /etc;
    #: checkouts use the project-local `.env`.
    environment_path: Path
    #: Where `backup create` writes when it is given no destination. The
    #: archives here hold private keys and the whole audit trail, so the
    #: directory is created `0700` and each archive `0600`.
    backup_dir: Path
    #: The plays core itself owns. A capability's play is not here: it ships
    #: inside that capability's wheel and reaches ``run_playbook`` as a path
    #: the package resolved for itself, so detaching the package takes the
    #: play with it and leaves no setting behind pointing at nothing. The
    #: origin check used to be the exception and is not any more — it is
    #: ``blitzecdn-origins``'. Decommissioning stays, because removing an edge
    #: has to work on an installation with no capability attached at all.
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
    #: Resolvers preflight asks, instead of whatever this host resolves with.
    #:
    #: Preflight predicts what the CA will see, and the CA resolves from the
    #: public internet. The controller's own resolver answers a different
    #: question: a split-horizon view, an internal forwarder, or a transparent
    #: proxy that claims every name will all disagree with the public answer
    #: while being perfectly healthy for every other purpose. Empty means fall
    #: back to the host resolver, which is right for an air-gapped controller
    #: with its own view of public DNS.
    preflight_dns_servers: tuple[str, ...] = ()
    #: Capabilities this installation's configuration depends on, by token.
    #:
    #: The deliberate answer to "an optional package was uninstalled while
    #: something still needed it". Optional capabilities are real distributions
    #: that can be attached and detached, so their absence is normal and must
    #: not be an error by itself — but an installation that has *decided* it
    #: needs one says so here, and the control plane then refuses to start
    #: without it rather than coming up and behaving as though the capability
    #: had simply been configured off.
    #:
    #: Generic on purpose. These are tokens matched against what the installed
    #: plugins declare in `PluginMetadata.provides`; core resolves them without
    #: knowing which distribution supplies any of them, so a capability nothing
    #: in this repository has heard of is checked exactly like `backup`.
    #:
    #: Empty by default: a fresh install requires no optional capability.
    required_capabilities: tuple[str, ...] = ()
    #: Candidate `BLITZE_*` variables that core itself does not consume.
    #:
    #: The generic answer to "an optional capability needs a credential". A
    #: MaxMind license key and an Under Attack signing secret used to be fields
    #: on this model, which meant core carried the name of a capability that
    #: may not be installed — and a package this repository has never heard of
    #: had no way to be configured at all. Core carries neither name. During
    #: composition, installed plugins explicitly claim keys through
    #: `AnsibleContribution.environment_keys`; unclaimed and multiply claimed
    #: names are configuration errors. Only the resolved subset reaches the
    #: package role through the subprocess environment.
    #:
    #: `SecretStr` because core cannot know which of these are credentials, and
    #: the safe assumption for a value it cannot interpret is that it is one:
    #: a traceback or a debugger that reprs `Settings` prints `**********`.
    #: Environment-only, like the API keys — `_read_project_config` refuses an
    #: unknown key, so none of these can come from the committed TOML.
    #:
    #: Core's own names are excluded, which keeps `BLITZE_API_KEY` and
    #: `BLITZE_API_KEYS` — controller authentication, no edge's business — out
    #: of every subprocess the control plane starts.
    capability_environment: dict[str, SecretStr] = Field(default_factory=dict)
    #: Per-origin budget for `blitzecdn origin check`. Short on purpose: an
    #: origin that needs longer than this to answer a bare TCP connect is one
    #: the edges will struggle with too.
    #: The controller's *own* advisory probe budget, inside certificate
    #: preflight. Not the fleet check's: that one is ``blitzecdn-origins``'
    #: role default, in that package's wheel.
    origin_check_timeout_seconds: int = Field(default=5, ge=1, le=60)
    #: How much of a run log to read back when showing an operator why a run
    #: failed. The log itself is never truncated — it is the full account — but
    #: an error message quoting all of it helps nobody.
    output_limit_bytes: int = Field(default=1_048_576, ge=4096, le=10_485_760)
    #: How many run logs to keep. They accumulate one per invocation — deploys,
    #: drift checks on a timer, every purge — so something has to bound them,
    #: and the alternative to a cap here is a disk that fills on a schedule.
    #: The newest are kept; a deployment whose log has aged out still has its
    #: structured result, which is what the control plane reasons from.
    run_log_retention: int = Field(default=500, ge=10, le=100_000)
    #: How many check-mode deployments and finished workflows to keep.
    #:
    #: The drift timer fires hourly and each firing writes a deployment row
    #: carrying a *complete* copy of every zone and record, so without a bound
    #: this table grows by a full desired state every hour whether or not
    #: anything changed. Real deployments are never pruned: they are the
    #: snapshots a rollback chooses from.
    history_retention: int = Field(default=1000, ge=50, le=100_000)
    certificate_reconcile_interval_seconds: int = Field(default=600, ge=0, le=86_400)
    certificate_renewal_interval_seconds: int = Field(default=43_200, ge=0, le=604_800)
    ssl_automatic_scan_interval_seconds: int = Field(
        default=2_592_000, ge=0, le=31_536_000
    )
    drift_check_interval_seconds: int = Field(default=3600, ge=0, le=86_400)
    redis_url: RedisDsn = RedisDsn("redis://127.0.0.1:6379/0")
    #: Wall-clock budget for one renewal sweep served over HTTP.
    #:
    #: A sweep runs certbot per site, each bounded only by
    #: `deployment_timeout_seconds`, so an unbounded sweep can hold its worker
    #: for hours. That is survivable on the CLI and on a timer, but over HTTP it
    #: is a request nothing will wait for and a worker nothing can reclaim. When
    #: the budget runs out the sweep stops between sites — never mid-issuance —
    #: and reports the ones it did not reach as skipped, because the next run
    #: picks them up and a truncated answer that says so is better than a
    #: connection that dies holding the lock.
    certificate_renewal_budget_seconds: int = Field(default=300, ge=30, le=7200)
    #: How many renewal sweeps may be in flight at once.
    #:
    #: Their own pool, not the server's. Renewal is the one HTTP operation that
    #: blocks for minutes, and left in the shared thread pool a handful of
    #: concurrent sweeps starve every other endpoint — including `/health`, so
    #: the controller would look dead to a load balancer while working
    #: perfectly. Small on purpose: issuance serialises on the deployment lock
    #: anyway, so more workers would only queue deeper.
    certificate_renewal_workers: int = Field(default=2, ge=1, le=16)
    allow_empty_sites: bool = False
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
        "environment_path",
        "backup_dir",
        "decommission_playbook_path",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: object) -> Path:
        return Path(str(value)).expanduser().resolve()

    @property
    def run_dir(self) -> Path:
        """Working files for an invocation in flight: its vars, its result.

        Derived rather than configurable. Everything in here is created and
        removed inside one run, so there is no reason for an operator to place
        it anywhere but beside the rest of the state.
        """
        return self.state_dir / "runs"

    @property
    def log_dir(self) -> Path:
        """Raw Ansible output, one file per run.

        Kept for maintenance, debugging and operator inspection. No application
        code reads these — what the control plane acts on comes from the
        Runner's structured events — so a missing or rotated-away log costs an
        explanation, never a decision.
        """
        return self.state_dir / "logs"

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

        try:
            values = cls._configuration_values(env, project_config, root)
            return cls.model_validate(values)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc

    @classmethod
    def _configuration_values(
        cls,
        env: Mapping[str, str],
        project_config: Mapping[str, object],
        root: Path,
    ) -> dict[str, object]:
        """Resolve every supported source into one validated model payload."""

        def value(environment_name: str, config_name: str, default: object) -> object:
            return env.get(environment_name, project_config.get(config_name, default))

        def path_value(environment_name: str, config_name: str, default: Path) -> Path:
            candidate = Path(str(value(environment_name, config_name, default)))
            return candidate if candidate.is_absolute() else root / candidate

        state = root / ".state"
        values: dict[str, object] = {
            "project_dir": root,
            "state_dir": state,
            "ansible_dir": root / "ansible",
            "inventory_path": root / "ansible/inventory/blitzecdn.yml",
            "playbook_path": root / "ansible/playbooks/edge.yml",
            "decommission_playbook_path": root / "ansible/playbooks/decommission.yml",
            "generated_vars_path": state / "desired-state.yml",
            "deployment_lock_path": state / "deployment.lock",
            "certificate_dir": state / "certificates",
        }
        for field, environment_name, config_name, relative_default in _PATH_SETTINGS:
            values[field] = path_value(
                environment_name, config_name, root / relative_default
            )
        for (
            field,
            environment_name,
            config_name,
            relative_default,
        ) in _STATE_PATH_SETTINGS:
            values[field] = path_value(
                environment_name, config_name, state / relative_default
            )
        for field, environment_name, config_name, default in _VALUE_SETTINGS:
            values[field] = value(environment_name, config_name, default)

        raw_email = value("BLITZE_ACME_DEFAULT_EMAIL", "acme_default_email", "")
        values.update(
            acme_default_email=(str(raw_email).strip().lower() if raw_email else None),
            preflight_dns_servers=cls._read_dns_servers(
                value("BLITZE_PREFLIGHT_DNS_SERVERS", "preflight_dns_servers", ())
            ),
            required_capabilities=cls._read_capabilities(
                value("BLITZE_REQUIRED_CAPABILITIES", "required_capabilities", ())
            ),
            capability_environment=cls._read_capability_environment(env),
            api_keys=cls._read_api_keys(env),
        )
        return values

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
    def _read_capabilities(raw: object) -> tuple[str, ...]:
        """Parse capability tokens from a comma-separated string or a list."""
        if isinstance(raw, str):
            parts: list[str] = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            parts = [str(part).strip() for part in raw]
        else:
            raise ValueError("required_capabilities must be a list or comma-separated")
        tokens: list[str] = []
        for part in filter(None, parts):
            if not part.replace("_", "").replace("-", "").isalnum():
                raise ValueError(
                    f"required capability {part!r} must be alphanumeric, "
                    "such as 'backup'"
                )
            if part not in tokens:
                tokens.append(part)
        return tuple(tokens)

    @staticmethod
    def _read_dns_servers(raw: object) -> tuple[str, ...]:
        """Parse resolver addresses from a comma-separated string or a list.

        Addresses only: a hostname here would have to be resolved by the very
        resolver we are trying not to trust.
        """
        if isinstance(raw, str):
            parts: list[str] = [part.strip() for part in raw.split(",")]
        elif isinstance(raw, (list, tuple)):
            parts = [str(part).strip() for part in raw]
        else:
            raise ValueError("preflight_dns_servers must be a list or comma-separated")
        servers: list[str] = []
        for part in filter(None, parts):
            try:
                ipaddress.ip_address(part)
            except ValueError as exc:
                raise ValueError(
                    f"preflight DNS server {part!r} is not an IP address"
                ) from exc
            servers.append(part)
        return tuple(servers)

    @staticmethod
    def _read_capability_environment(env: Mapping[str, str]) -> dict[str, SecretStr]:
        """Stage non-core `BLITZE_*` values for contribution-owner resolution.

        Read from the *merged* environment, so a value in the controller's
        `.env` reaches a deploy without an operator having to export it into
        every shell — which is the whole reason this is collected here rather
        than left to `os.environ` in the executor.

        The plugin resolver interprets ownership, not values. It accepts only
        keys explicitly declared by one installed package and rejects typos,
        detached-package settings and ownership collisions before a runner is
        built. Thus collecting candidates here is not a forwarding policy.
        """
        return {
            name: SecretStr(value)
            for name, value in sorted(env.items())
            if name.startswith("BLITZE_") and name not in _CORE_ENVIRONMENT_KEYS
        }

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
