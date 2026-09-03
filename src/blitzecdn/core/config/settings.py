"""The validated settings model.

What a setting means, what it may hold, and what it is when unset. Where a
value *comes from* is :mod:`blitzecdn.core.config.loading`'s question.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import Field, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from blitzecdn.core.config.loading import settings_payload
from blitzecdn.core.exceptions import ConfigurationError


class Settings(BaseSettings):
    """Validated application settings assembled from environment and TOML.

    :mod:`blitzecdn.core.config.loading` owns source precedence and the path
    defaults that are relative to ``project_dir``; ``from_environment`` is the
    seam between the two and does nothing but validate what that module
    assembled.  ``BaseSettings`` owns typed coercion and the settings model
    itself, so booleans, integers and secrets no longer need a parallel
    hand-written conversion layer.
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
    #: The plays core itself owns. A capability's play is not here: it ships
    #: inside that capability's wheel and reaches ``run_playbook`` as a path
    #: the package resolved for itself, so detaching the package takes the
    #: play with it and leaves no setting behind pointing at nothing. The
    #: origin check used to be the exception and is not any more — it is
    #: ``blitzecdn-origins``'. Decommissioning stays, because removing an edge
    #: has to work on an installation with no capability attached at all.
    decommission_playbook_path: Path
    ansible_playbook: str = "ansible-playbook"
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
    #: composition, installed plugins explicitly claim names through
    #: `ConfigurationContribution` — as an `EnvironmentKey` when the value is a
    #: secret and a `CapabilitySetting` when it is not; unclaimed and multiply
    #: claimed names are configuration errors. Only the resolved *secrets*
    #: reach the package role through the subprocess environment.
    #:
    #: `SecretStr` because core cannot know which of these are credentials, and
    #: the safe assumption for a value it cannot interpret is that it is one:
    #: a traceback or a debugger that reprs `Settings` prints `**********`.
    #: Not environment-only any more: a `blitzecdn.toml` key core does not
    #: recognise is staged here as `BLITZE_<KEY>` rather than refused outright,
    #: because a non-secret capability setting has to be writable in the file
    #: that holds every other non-secret default. The refusal did not
    #: disappear, it moved — an unclaimed name is rejected by the plugin
    #: resolver, which is the only thing that knows what is claimed.
    #:
    #: Core's own names are excluded, which keeps `BLITZE_API_KEY` and
    #: `BLITZE_API_KEYS` — controller authentication, no edge's business — out
    #: of every subprocess the control plane starts.
    capability_environment: dict[str, SecretStr] = Field(default_factory=dict)
    #: Capability configuration from `blitzecdn.toml`: keys core does not
    #: recognise, staged under the `BLITZE_*` name they correspond to.
    #:
    #: Separate from `capability_environment` because of where it came from.
    #: A non-secret capability setting belongs in the non-secret file — a
    #: renewal interval is exactly what it exists to hold — but a *secret*
    #: must not be settable there, and the two are indistinguishable until a
    #: plugin says which is which. Keeping the origins apart lets the resolver
    #: honour that: a `CapabilitySetting` reads from both, an `EnvironmentKey`
    #: only from the environment. Plain strings, never `SecretStr`, because
    #: nothing that reaches this map is allowed to be a credential.
    #:
    #: Lower precedence than the environment, like every other source here.
    capability_config_file: dict[str, str] = Field(default_factory=dict)
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
    drift_check_interval_seconds: int = Field(default=3600, ge=0, le=86_400)
    redis_url: RedisDsn = RedisDsn("redis://127.0.0.1:6379/0")
    #: How many route handlers may occupy the API's offload pool at once.
    #:
    #: Their own pool, not the server's. Some operations block for minutes —
    #: a certificate renewal sweep is the standing example — and left in the
    #: shared thread pool a handful of them starve every other endpoint,
    #: including `/health`, so the controller would look dead to a load
    #: balancer while working perfectly.
    #:
    #: This is core's, and it stayed core's when the certificate settings
    #: around it left. It was called `certificate_renewal_workers`, which read
    #: as one capability's setting and was not: the pool is API infrastructure,
    #: a package cannot create an application-scoped resource from a
    #: registration hook, and an installation with no `blitzecdn-certificates`
    #: still has the pool and still wants it bounded. Only the name named a
    #: capability.
    #:
    #: Small on purpose: the work it carries serialises on the deployment lock
    #: anyway, so more workers would only queue deeper.
    api_worker_threads: int = Field(default=2, ge=1, le=16)
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

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        project_dir: Path | None = None,
    ) -> Self:
        values = settings_payload(environment, project_dir=project_dir)
        try:
            return cls.model_validate(values)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid BlitzeCDN configuration: {exc}") from exc

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
