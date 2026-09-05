"""The values plugins hand back, and the identity they hand back with them.

Everything here is a frozen dataclass rather than a dictionary. A contribution
crosses a package boundary — often one shipped as a separate distribution — so
"which keys does this have" has to be answerable by reading a type rather than
by reading whichever plugin happened to produce the value.

Nothing in this module imports a capability. `core` is what a capability builds on;
the two runtime-bound hooks that need built services take the composition root
as an argument instead, which keeps the arrow pointing one way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from typer import Typer

#: The pluggy project name. It prefixes every hook, which is what lets one
#: manager host hooks from packages that never heard of each other.
PROJECT_NAME = "blitzecdn"

#: The entry-point group an external distribution advertises itself in.
ENTRY_POINT_GROUP = "blitzecdn.plugins"

#: The hook contract this control plane speaks. Bumped only when a hookspec
#: changes shape in a way an existing implementation cannot survive.
HOOK_API_VERSION = 1

#: Every contract version this control plane still accepts a plugin for.
#:
#: A set rather than a comparison against `HOOK_API_VERSION`, because the gate
#: used to be `!=` and that made bumping the contract a same-day fork of every
#: wheel in existence: v2 core, and every plugin declaring v1 refused at once,
#: with no release in which an author could support both. Widening this to
#: `{1, 2}` is what a deprecation window *is* — v1 plugins keep loading while
#: their authors move, and dropping 1 later is a second, separate decision that
#: shows up in this line.
#:
#: `pip` already refuses a wheel whose `blitzecdn>=3.0.0,<4` cannot be
#: satisfied, so this is the second lock rather than the first. It earns its
#: place on the installs that never went through a resolver — an editable
#: checkout, a vendored tree — where the alternative is an AttributeError
#: inside a hook call that names no plugin.
SUPPORTED_HOOK_API_VERSIONS = frozenset({1})

#: What a contribution is allowed to be. Ansible variables are YAML, so this is
#: what `yaml.safe_dump` will accept and what the edge roles can read back.
type StateValue = (
    str
    | int
    | float
    | bool
    | Mapping[str, "StateValue"]
    | tuple["StateValue", ...]
    | list["StateValue"]
    | None
)


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Who a plugin is, and what happens when it cannot be loaded.

    `required` is the whole of the failure policy. A required plugin that
    fails to import or register stops the process at startup, because the
    control plane without it is not a degraded control plane but a wrong one —
    a `dns` that did not load would silently render an empty fleet. An optional
    plugin's failure is reported with its name and the process continues.
    """

    name: str
    version: str
    #: The hook contract this plugin was *written against*, as a literal.
    #:
    #: Required, and deliberately so. It defaulted to `HOOK_API_VERSION` —
    #: core's own constant, read at the plugin's import time — which meant a
    #: plugin that said nothing always agreed with whatever core it was loaded
    #: into, however old the hooks it implements. Every one of the twenty-three
    #: plugins in this workspace omitted it, so the check had never once been
    #: able to fail. The author who was explicit and pinned `1` was the only
    #: one it could ever refuse: exactly backwards.
    #:
    #: Write the number, not `HOOK_API_VERSION`. Importing the constant
    #: reintroduces the same defect one level up —
    #: `test_a_plugin_states_the_hook_contract_it_was_written_against` refuses
    #: it for the plugins in this workspace, and it is why this field cannot
    #: have a default: there is no value core can supply that means "whatever
    #: the author had in front of them".
    api_version: int
    required: bool = False
    summary: str = ""
    #: The capability tokens this plugin supplies, for configuration that has
    #: to say what it depends on. Empty means "just my own name", which is the
    #: right answer for almost every plugin: `backup` provides `backup`.
    #:
    #: It is separate from `name` because the two answer different questions.
    #: `name` identifies the *plugin* — it is how a duplicate is detected and a
    #: failure attributed — while a token names a *capability*, which is what a
    #: site or a fleet setting actually depends on. One plugin may supply
    #: several, and a replacement implementation may supply a token another
    #: package used to, under its own name.
    provides: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"plugin name {self.name!r} must be alphanumeric")
        for token in self.provides:
            if not token or not token.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"capability {token!r} must be alphanumeric")

    @property
    def capabilities(self) -> frozenset[str]:
        """Every capability token this plugin answers for, its name included."""
        return self.provides | {self.name}


@dataclass(frozen=True, slots=True)
class EdgeModule:
    """One Nginx dynamic module an installed capability needs on the edge.

    A dynamic module has two halves, and they are answered in two different
    places. It has to be *in the image* — built against that exact Nginx ABI,
    or already shipped by the official base — and it has to be *loaded* by the
    running configuration, which is one `load_module` list the whole process
    shares. Both halves used to be written into the edge image's own build
    context, which made that build a third register of which capabilities
    exist — and the only one that kept naming a capability after its
    distribution was detached, because an image is built once and pinned by
    digest: an edge with no `blitzecdn-geoip` still loaded the GeoIP2 module.

    Declaring it here answers both from one place. Core composes the resolved
    set into the `load_module` list `blitzecdn_nginx` renders onto the host, so
    an edge loads exactly what is installed; and `blitzecdn edge image spec`
    emits the same set as the build arguments the image is built from, so the
    Dockerfile enumerates nothing.

    `name` is the module as Nginx's own pkg-oss tooling names it — `geoip2`,
    `brotli`, `njs` — because that name is what the build takes. `objects` are
    the shared objects to load, in order, and there may be more than one:
    `brotli` builds a filter and a static module and only the filter is loaded.
    Core supplies the directory they live in; a capability names the files.

    `build` is the difference between a module the image has to compile and one
    the official base image already carries. njs is the second kind, and the
    distinction cannot be inferred from the name: asking pkg-oss to build a
    module that is already installed is a build failure, and omitting one that
    is not is an edge that fails `nginx -t` on its first deploy.

    `probe` is one directive the module registers, evaluated by the image's
    build-time probe. A module that loads but registers nothing is
    indistinguishable from a working one until an edge configuration uses it,
    which is far too late — and only the capability knows which directive is
    its.
    """

    name: str
    objects: tuple[str, ...]
    build: bool = True
    probe: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"module name {self.name!r} must be alphanumeric")
        if not self.objects:
            raise ValueError(f"module {self.name!r} loads no shared object")
        for shared_object in self.objects:
            if Path(shared_object).name != shared_object or not shared_object.endswith(
                ".so"
            ):
                raise ValueError(
                    f"module {self.name!r} names the shared object "
                    f"{shared_object!r}; it must be a plain '.so' filename, "
                    "resolved against the directory the image puts modules in"
                )


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    """One `BLITZE_*` name an installed capability claims, and its shape.

    This used to be a bare string, and a bare string answers only half the
    question. It says the name is this package's — which is what stops a typo,
    a detached package's leftover setting and two packages claiming one name —
    and it says nothing about whether the capability can work without it or
    what a usable value looks like. Every package therefore answered that half
    itself, in its own module, in its own way, and at its own moment: the
    security capability re-spelled its own key in a constant, read it back off
    `Settings` through an untyped `getattr`, and enforced a 32-byte minimum in
    a deployment check — so a controller configured with a placeholder secret
    started, converged, and only reported the mistake when a site turned Under
    Attack Mode on.

    Declaring the shape here moves each of those to the one moment they are
    all cheap: composition, before an adapter exists or a play could start.
    Core enforces the two rules it can enforce without knowing what any of
    these values *mean* — presence, and a minimum length — and hands the
    package the rest as its own typed `CapabilityConfig`.

    `required` is presence, and it is not the same question as whether the
    capability is useful. A required key is one whose absence makes the
    installed package *wrong* rather than idle, and it stops the control plane
    at startup naming the key and the package. Under Attack Mode is
    deliberately not that: a controller with no signing secret is a perfectly
    good control plane with one site setting it will refuse, which is a
    deployment check's answer and not a startup failure. Almost every key
    should leave this alone.

    `minimum_bytes` is checked only when a value is *present*, which is what
    keeps those two rules independent. It is for a value whose length is the
    whole of its validity — an HMAC secret below the hash's block size buys
    nothing, and a short one is nearly always a placeholder somebody meant to
    replace. A value with any richer rule than that stays the package's to
    validate: core cannot know what a MaxMind account id looks like, and a
    core that grew a way to describe one would be carrying the shape of a
    capability that may not be installed.

    `summary` is what an operator is shown — by the refusal that names a
    missing key, and by ``blitzecdn plugins`` — so write it as the sentence
    that would tell somebody where to get the value.
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


#: What a capability's non-secret setting is allowed to be. Four types, because
#: four is what the settings that exist actually need — a count, a flag, a name
#: and a location — and because each one has an unambiguous spelling as the
#: string an environment variable or a TOML scalar arrives as. A capability
#: wanting something richer is describing a structure, and a structure belongs
#: in that capability's own file rather than in a `BLITZE_*` name.
type SettingValue = str | int | bool | Path


@dataclass(frozen=True, slots=True)
class CapabilitySetting:
    """One non-secret `BLITZE_*` name an installed capability claims.

    :class:`EnvironmentKey`'s counterpart, and the half that was missing.
    Together they are the whole of what a capability may ask an operator to
    configure — a secret whose value core must never look at, and a setting
    whose value core resolves, type-checks and hands back.

    The absence of this class is why the rule it enforces was, until now,
    documentation rather than architecture. "An optional capability's
    configuration is not a field on ``Settings``" was written down and true of
    exactly one capability: ``blitzecdn-security``, whose only configuration is
    a secret, which was the only kind that could be declared. Every non-secret
    one had nowhere else to go, so nine of them stayed on ``Settings`` —
    ``certbot``, an ACME email, four renewal intervals, a backup directory —
    each one a field the core distribution carries for a wheel that may not be
    installed, and each one unreachable for the capability that owns it without
    reading a model core owns.

    `default` carries the type as well as the value, so a declaration cannot
    say ``int`` and mean ``"2"``. That is also why there is no ``kind`` beside
    it: two ways to say the same thing is two ways for them to disagree.

    A `Path` default is resolved against the controller's state directory when
    it is relative, which is the only sensible reading of a location a
    capability names before it knows where this controller keeps its state —
    and it is exactly what core already did for its own ``backup_dir``. An
    absolute default is taken as written.

    `minimum` and `maximum` are the bounds core can enforce without knowing
    what the value *means*, and they apply to an ``int`` only. They exist
    because the settings being moved here already had them: a renewal interval
    of zero disables the job, and a negative one is nonsense that would reach
    APScheduler as a trigger it cannot build. Anything richer stays the
    package's — core cannot know that an ACME directory URL must be an ACME
    directory URL, and a core that grew a way to say so would be carrying the
    shape of a capability that may not be installed.

    `summary` is what an operator is shown by ``blitzecdn plugins``, so write
    it as the sentence that explains what changing the value would do.
    """

    name: str
    default: SettingValue
    summary: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: Whether this setting survives a restore onto a *different* host.
    #:
    #: Almost everything does: an interval, an executable name and a CA
    #: identity are decisions an operator made, and losing them on recovery is
    #: losing configuration. What must not travel is a value describing the
    #: machine — ``blitzecdn-backup``'s own archive directory is the standing
    #: case, and restoring it would point a rebuilt controller's backups at a
    #: path belonging to the host that died.
    #:
    #: Declared here because core cannot tell. It knows which of *its own*
    #: settings describe a machine; a capability's are the capability's to
    #: classify, and a controller may have capabilities installed that this
    #: repository has never heard of.
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
    """Everything one installed capability asks an operator to configure.

    One contract for both halves, because an operator asking "what does this
    capability need set up" is asking one question, and ``blitzecdn plugins``
    answers it from one place. They were briefly going to be two — a new hook
    for settings beside the existing ``environment_keys`` on
    ``AnsibleContribution`` — which would have left the answer split across two
    contracts, one of them named after a subsystem that half the values never
    reach.

    Both lists are claims, and a claim is what makes ownership decidable. Core
    stages every non-core ``BLITZE_*`` name it can see, from the environment,
    from ``.env`` and from ``blitzecdn.toml``, and then refuses any that no
    installed capability claims: a typo, a setting left behind by a package
    that was detached, or a package that was never installed. Without the
    claim, all three are indistinguishable from a value that is simply being
    ignored, which is how an operator spends an afternoon on a credential that
    was reaching nothing.
    """

    plugin: str
    #: Secrets. Only these are copied into Ansible's subprocess environment,
    #: they remain ``SecretStr`` throughout, and they never travel through argv
    #: or desired state.
    environment_keys: tuple[EnvironmentKey, ...] = ()
    #: Non-secrets. Resolved to the declared type and handed back through
    #: :class:`~blitzecdn.core.plugins.resolution.CapabilityConfig`.
    settings: tuple[CapabilitySetting, ...] = ()


@dataclass(frozen=True, slots=True)
class AnsibleContribution:
    """The Ansible roles one installed plugin brings with it.

    A capability is not complete until the deployment implementation travels
    with it. A package that owns a role ships the role inside its own wheel and
    answers this hook with the directory it landed in; the control plane adds
    that directory to Ansible's role search path and learns nothing else.

    Every member is there because the thing it describes is *global*.
    `roles_path` answers where Ansible resolves a role name, which is one
    process-wide list every play shares and core therefore has to compose.
    `edge_roles`, `host_roles` and `teardown_roles` answer which of those roles
    core's own plays run, which is one ordered list per slot core has to
    compose for the same reason: a capability cannot converge an edge by
    shipping a role nothing ever includes, and the plays that would include it
    are core's.

    `edge_modules` is the same question about Nginx's own extension point:
    `load_module` is a main-context directive, so the modules an edge loads are
    one list per process and core has to compose that too. See
    :class:`EdgeModule` for why the image build reads it as well.

    A playbook is not global and is deliberately absent: the package that owns
    a play already passes its path to ``PlaybookRunner.run_playbook``. Adding
    `playbooks_path` or `collections_path` before something needs them would be
    describing a requirement that does not exist.

    Every role list names roles this contribution's own `roles_path` ships —
    core refuses a name that is not there rather than letting the play fail
    much later with "the role was not found".

    Two of the slots are in the edge play, and there are two of them because
    there are two answers to *when*, and they are opposites rather than
    preferences.

    `edge_roles` run after the host has an engine, a runtime image and its
    persistent directories, and before the firewall opens a port or
    ``blitzecdn_nginx`` renders and validates the configuration tree. That is
    where a capability contributing *something the configuration then depends
    on* has to be — a database, an njs module, a snippet in `conf.d` — because
    all of it must exist before `nginx -t` decides whether the tree may be
    served.

    `host_roles` run at the end, after ``blitzecdn_edge_stack`` has the edge
    serving. That is where a capability configuring the *host underneath* the
    runtime has to be, and the SSH hardening this slot exists for shows why it
    cannot be the other one: a host that fails firewall validation must never
    be left key-only and unreachable from the management network, which is
    exactly what an earlier slot would do to it. A role here reads nothing the
    renderer produced and contributes nothing it reads; it is on the far side
    of the edge being up.

    The third slot is in a different play altogether. `teardown_roles` run in
    the decommission play, and they answer the question the other two leave
    open: a capability that put something on a host has to be able to take it
    off again. Core cannot do it on the capability's behalf — it would have to
    name a path belonging to a wheel that may not be installed, in a role that
    is always installed — and a host is usually decommissioned by a controller
    whose package set has drifted from the one that converged it, so "the
    capability is still attached" is not something the removal may depend on
    either. What core owns instead is the position: the slot runs *before*
    ``blitzecdn_teardown``, so that role's clean-host assertion is the last
    word on the whole decommission rather than a verdict passed before half
    the removal happened.

    A capability declares any of them, or none. Core enforces the ordering by
    position in its own plays and never learns a role's name.

    `plugin` is here for the failure message and for ordering. Two packages
    shipping a role of the same name is a conflict that must be reported with
    both names rather than resolved by whichever happened to register last.

    Six members was where this stopped being obviously one object, and the
    split has happened — though not along the seam that was predicted. The
    pressure did not come from a fourth lifecycle slot; it came from
    configuration. ``environment_keys`` used to be the sixth member here, and
    the moment a capability needed to declare a *non-secret* setting as well,
    the honest question was why either lived on a contract named after
    Ansible. A secret was forwarded into Ansible's subprocess environment, so
    it had a reason to be here; an interval a scheduler reads on the
    controller never touches Ansible at all. Both are now
    :class:`ConfigurationContribution`, and this contract is back to five
    members, every one of which describes a file on an edge.
    """

    plugin: str
    roles_path: Path
    #: Role names, from this contribution's own directory, that core's edge
    #: play runs. Empty for a package whose roles are only ever reached by its
    #: own plays — ``blitzecdn-cache`` purges through a play of its own and
    #: converges nothing on a deploy.
    edge_roles: tuple[str, ...] = ()
    #: Role names, from this contribution's own directory, that core's edge
    #: play runs in its *host* slot — after the edge is serving, for a
    #: capability that configures the host rather than the runtime.
    #: ``blitzecdn-hardening`` is the whole of the current use: SSH policy and
    #: a Fail2Ban jail, neither of which the rendered configuration reads.
    host_roles: tuple[str, ...] = ()
    #: Role names, from this contribution's own directory, that core's
    #: decommission play runs before ``blitzecdn_teardown``, to remove what
    #: this capability put on the host. Empty for a capability that writes
    #: nothing outside the trees core already removes — the data directory,
    #: the state tree, and any systemd unit matching the managed prefix.
    teardown_roles: tuple[str, ...] = ()
    #: Nginx dynamic modules this capability's configuration needs loaded.
    #: Global for the same reason the role lists are: `load_module` is a
    #: main-context directive and there is one list per Nginx process, so core
    #: composes it. Empty for a capability that adds no module — most of them.
    edge_modules: tuple[EdgeModule, ...] = ()


@dataclass(frozen=True, slots=True)
class NginxContribution:
    """Static Nginx template fragments shipped by an installed package.

    The four contexts are deliberately structural rather than directive-level:
    one global ``http`` resource, one server-level insertion point, one access
    phase before dispatch, and one upstream location after dispatch.  They are
    enough for current capabilities without allowing a package to replace a
    complete server block or turning Pluggy into a templating API.
    """

    plugin: str
    templates_path: Path
    http_fragments: tuple[str, ...] = ()
    server_fragments: tuple[str, ...] = ()
    access_fragments: tuple[str, ...] = ()
    upstream_fragments: tuple[str, ...] = ()


class ProcessKind(StrEnum):
    """Which long-running process is starting.

    Startup work is not the same in all of them: republishing queued
    deployments belongs to the API, which is the one process that owns the
    deployment lock for the lifetime of the node, and running it from every
    CLI invocation would take that lock a hundred times a day.
    """

    API = "api"
    CLI = "cli"
    WORKER = "worker"
    SCHEDULER = "scheduler"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """What a lifecycle contribution is told about the process it runs in."""

    process: ProcessKind
    #: `Settings`, untyped here only because `core.plugins` sits underneath
    #: `core.config` in no particular order and the value is opaque to this
    #: module. Every implementation annotates it concretely.
    settings: object


@dataclass(frozen=True, slots=True)
class CliCommandGroup:
    """One Typer application to graft onto the root command.

    `name` is the sub-command it appears under. `None` means the group's
    commands are root commands — `deploy`, `plan`, `rollback`, `drift` are
    verbs an operator types directly, and nesting them under a noun to satisfy
    the registration mechanism would be the mechanism dictating the interface.
    """

    name: str | None
    app: Typer


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One reason `/health` may answer "unavailable", and its name.

    The callable raises to fail. It returns nothing on success: a check that
    wanted to report a value would be a metric, and `/metrics` is where those
    go.
    """

    name: str
    check: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Recurring maintenance, and the only description of it there is.

    The scheduler publishes this job's `name` to the queue and the worker
    resolves it back to `run` through the registry in its own process. That
    indirection is not a service locator: the name is a durable message
    payload crossing a process boundary, so it cannot be a function reference,
    and the table it is resolved against is this one.

    `interval_seconds` of zero disables the job, which is how every one of them
    is turned off by configuration.
    """

    name: str
    interval_seconds: int
    run: Callable[[str], None]
    jitter_seconds: int = 0
    #: How long the queue's single-flight key is held, so a scheduler that
    #: fires again while the previous run is still going does not stack them.
    #:
    #: Zero means twice the interval, which is the right answer for every job
    #: that has no opinion: a run that has not finished by then is stuck, not
    #: slow. A job declares a number only when it can outlast that — work
    #: bounded by its own budget rather than by its cadence.
    lease_seconds: int = 0


@dataclass(frozen=True, slots=True)
class SiteStateContribution:
    """One plugin's share of the Ansible document for one virtual host.

    `overrides` is what makes merging order-independent. Two plugins writing
    the same variable is a conflict unless exactly one of them says it is
    replacing a value another produced — `certificates` deliberately replaces
    the certificate paths projected from the site model, and saying so is the
    difference between a designed override and whichever plugin loaded last.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class FleetStateContribution:
    """Variables about the fleet as a whole rather than about one site.

    Separate from `SiteStateContribution` because it is derived from every
    site at once: which single site carries `reuseport` on the QUIC listener
    is not a fact any one site knows about itself.
    """

    plugin: str
    variables: Mapping[str, StateValue]
    overrides: frozenset[str] = field(default_factory=frozenset)


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Something a plugin knows about a site that a deployment should hear.

    A blocking issue refuses the deployment before anything is rendered; a
    warning is reported and converged anyway.
    """

    plugin: str
    site: str
    message: str
    severity: Severity = Severity.BLOCKING


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Every plugin's answer about one site, and whether it may be deployed."""

    site: str
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is Severity.BLOCKING
        )

    @property
    def ok(self) -> bool:
        return not self.blocking


__all__ = [
    "ENTRY_POINT_GROUP",
    "HOOK_API_VERSION",
    "PROJECT_NAME",
    "SUPPORTED_HOOK_API_VERSIONS",
    "AnsibleContribution",
    "CapabilitySetting",
    "CliCommandGroup",
    "ConfigurationContribution",
    "EdgeModule",
    "EnvironmentKey",
    "FleetStateContribution",
    "HealthCheck",
    "NginxContribution",
    "PluginMetadata",
    "ProcessKind",
    "RuntimeContext",
    "ScheduledJob",
    "SettingValue",
    "Severity",
    "SiteStateContribution",
    "StateValue",
    "ValidationIssue",
    "ValidationResult",
]
