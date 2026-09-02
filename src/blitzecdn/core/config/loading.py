"""Source precedence: how a `Settings` payload is assembled, and from where.

The model in :mod:`blitzecdn.core.config.settings` says what a setting *is* —
its type, its bounds, its default. This module says where the value comes
from, and the answer is a chain rather than a lookup: a CLI argument has
already been applied by the caller, then a `BLITZE_*` environment variable,
then the project-local `.env` staged as defaults that never override an
exported name, then `blitzecdn.toml`, then the field default. Keeping it here
rather than on the model is what makes that chain testable on its own, without
constructing a validated `Settings` first.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import SecretStr

from blitzecdn import ansible as core_ansible
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


def settings_payload(
    environment: Mapping[str, str] | None = None,
    *,
    project_dir: Path | None = None,
) -> dict[str, object]:
    """Assemble one `Settings` payload from every configured source.

    Resolving the project root twice is deliberate: `.env` is read relative to
    the root the process already knows, and only then may that file move the
    root by setting `BLITZE_PROJECT_DIR` itself. An explicit `project_dir`
    argument outranks both.
    """
    supplied = os.environ if environment is None else environment
    initial_root = (
        project_dir or Path(supplied.get("BLITZE_PROJECT_DIR", Path.cwd()))
    ).resolve()
    env = _with_local_environment(supplied, initial_root)
    root = (project_dir or Path(env.get("BLITZE_PROJECT_DIR", initial_root))).resolve()
    project_config = _read_project_config(
        Path(env.get("BLITZE_CONFIG", root / "blitzecdn.toml"))
    )
    try:
        return _configuration_values(env, project_config, root)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc


def _configuration_values(
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
        # Not derived from `root`. The platform's Ansible ships inside
        # this wheel and is located through `importlib.resources`, so these
        # four resolve identically in a checkout and on a controller that
        # has no checkout at all. They are deliberately not operator
        # configurable: pointing the control plane at someone else's copy
        # of the platform roles is not a supported deployment.
        "ansible_dir": core_ansible.ROLES_PATH.parent,
        "inventory_path": core_ansible.INVENTORY_PATH,
        "playbook_path": core_ansible.EDGE_PLAYBOOK,
        "decommission_playbook_path": core_ansible.DECOMMISSION_PLAYBOOK,
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
        preflight_dns_servers=_read_dns_servers(
            value("BLITZE_PREFLIGHT_DNS_SERVERS", "preflight_dns_servers", ())
        ),
        required_capabilities=_read_capabilities(
            value("BLITZE_REQUIRED_CAPABILITIES", "required_capabilities", ())
        ),
        capability_environment=_read_capability_environment(env),
        api_keys=_read_api_keys(env),
    )
    return values


def _with_local_environment(
    environment: Mapping[str, str], project_dir: Path
) -> dict[str, str]:
    """Load project-local defaults without overriding the process environment."""
    merged = dict(environment)
    path = project_dir / ".env"
    if not path.exists():
        return merged
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
        merged.setdefault(name, parts[0] if parts else "")
    return merged


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
                f"required capability {part!r} must be alphanumeric, such as 'backup'"
            )
        if part not in tokens:
            tokens.append(part)
    return tuple(tokens)


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
