"""Fold what the installed plugins contribute into the facts core owns.

Every function here answers the same question about a different subsystem:
*given the contributions of whatever happens to be installed, what is the one
process-wide value core has to hand its adapters?* Ansible resolves a role name
against a single search path. A play runs one ordered list of roles per slot.
`load_module` is a main-context directive, so an edge loads one list of dynamic
modules. An Nginx fragment name is claimed once. A `BLITZE_*` variable has one
owner. None of those is a per-capability value that a capability could own, and
that is exactly why core composes them.

The rules are the same in every one of them, and they are here together so they
stay the same. Contributions are ordered by plugin name, never by the order
pluggy registered them, so two controllers with the same packages installed
resolve the same value. A conflict is refused with *both* owners named rather
than resolved by first-wins, because first-wins here means a package silently
replacing `blitzecdn_nginx` or an edge loading whichever `geoip2` was emitted
first. A contribution that names something its own wheel does not carry is
refused now, with the distribution named, rather than much later by Ansible
with only the role named — and in the decommission slot, later means after the
play has begun taking a host apart.

These used to live in two modules named after their consumers:
`core/ansible/roles.py` and `core/nginx.py`. Neither name was true of the whole
of what it held — the capability *environment* resolver sat in the Nginx module
and had nothing to do with Nginx — and the shared rules above had three copies
of their reasoning. The consumers are still Ansible and Nginx; the *job* is
plugin composition, so it lives with the plugin mechanism.

What none of this does is copy anything. A staging directory would mean the
roles that actually run are a snapshot of the roles the packages installed,
which is a second source of truth and a stale one every time a package is
upgraded without a redeploy. The package's directory *is* the role.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, PluginError
from blitzecdn.core.plugins.types import (
    AnsibleContribution,
    CapabilitySetting,
    ConfigurationContribution,
    EnvironmentKey,
    NginxContribution,
    SettingValue,
)

__all__ = [
    "CapabilityConfig",
    "ResolvedCapabilityEnvironment",
    "ResolvedEdgeModule",
    "ResolvedNginxResource",
    "resolve_capability_environment",
    "resolve_edge_capability_roles",
    "resolve_edge_modules",
    "resolve_host_capability_roles",
    "resolve_nginx_resources",
    "resolve_plugin_configuration",
    "resolve_role_search_path",
    "resolve_teardown_capability_roles",
]

_CONTEXTS = ("http", "server", "access", "upstream")


@dataclass(frozen=True, slots=True)
class ResolvedNginxResource:
    """One validated resource, ready to pass to the renderer."""

    plugin: str
    name: str
    template: Path


def resolve_nginx_resources(
    contributions: Iterable[NginxContribution],
) -> dict[str, tuple[ResolvedNginxResource, ...]]:
    """Validate paths and ownership, then order resources by plugin and name."""
    resolved: dict[str, list[ResolvedNginxResource]] = {
        context: [] for context in _CONTEXTS
    }
    owners: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        root = contribution.templates_path
        if not root.is_dir():
            raise PluginError(
                f"plugin {contribution.plugin!r} contributes the Nginx templates "
                f"directory {root}, which does not exist"
            )
        for context in _CONTEXTS:
            names = getattr(contribution, f"{context}_fragments")
            for name in names:
                relative = Path(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or relative.name != name
                ):
                    raise PluginError(
                        f"plugin {contribution.plugin!r} contributes invalid Nginx "
                        f"resource name {name!r}; names must be plain filenames"
                    )
                if name in owners:
                    raise PluginError(
                        f"Nginx resource {name!r} is contributed by both plugin "
                        f"{owners[name]!r} and plugin {contribution.plugin!r}"
                    )
                template = root / name
                if not template.is_file():
                    raise PluginError(
                        f"plugin {contribution.plugin!r} contributes Nginx resource "
                        f"{name!r}, but {template} is not a file"
                    )
                owners[name] = contribution.plugin
                resolved[context].append(
                    ResolvedNginxResource(contribution.plugin, name, template)
                )
    return {context: tuple(resources) for context, resources in resolved.items()}


@dataclass(frozen=True, slots=True)
class CapabilityConfig:
    """One installed package's own configuration, and nothing else's.

    A capability that needs a credential used to reach for
    ``Settings.capability_environment`` itself — an untyped ``getattr`` against
    a model core owns, returning every claimed key in the installation, out of
    which the package picked its own by re-spelling the name. It worked, and
    every part of it was a copy waiting to drift: the name in two places, the
    read in a helper each package wrote again, and no answer at all to "what
    does this capability need" for anything that had to ask.

    This is that read, done once by core and handed back scoped: the keys *this
    plugin declared*, resolved from the merged environment, with an unset one
    present as an empty secret rather than absent. A name the package did not
    declare is refused rather than returned empty, because the two mistakes
    look identical at the call site and only one of them is a typo.
    """

    plugin: str
    values: Mapping[str, SecretStr]
    #: The non-secret half, already coerced to each setting's declared type.
    #: Separate from `values` because the two are read differently and shown
    #: differently: a secret is only ever asked whether it is *set*, while a
    #: setting is asked for its value and has one whether or not an operator
    #: supplied it.
    settings: Mapping[str, SettingValue] = field(default_factory=dict)

    def secret(self, name: str) -> SecretStr:
        """This key's value, empty when the controller has not set it."""
        if name not in self.values:
            raise PluginError(
                f"plugin {self.plugin!r} reads the environment key {name!r}, "
                "which it does not declare. A secret is claimed once, in its "
                "`ConfigurationContribution.environment_keys`, and read by "
                "that name."
            )
        return self.values[name]

    def is_set(self, name: str) -> bool:
        """Whether the controller supplied a value for this declared key."""
        return bool(self.secret(name).get_secret_value())

    def _setting(self, name: str, kind: type) -> SettingValue:
        """One declared setting, checked against the type the caller expects.

        The check is not redundant with the coercion that produced the value.
        That one held the value to the *declaration*; this one holds the
        declaration to the *call site*, and the two are written in different
        files by different people. `integer()` on a setting somebody later
        redeclared as a path is a mistake worth a sentence rather than a
        `TypeError` several frames away.
        """
        if name not in self.settings:
            raise PluginError(
                f"plugin {self.plugin!r} reads the setting {name!r}, which it "
                "does not declare. A setting is claimed once, in its "
                "`ConfigurationContribution.settings`, and read by that name."
            )
        value = self.settings[name]
        # `bool` before `int`, as everywhere this type is inspected.
        matches = (
            isinstance(value, bool) if kind is bool else isinstance(value, kind)
        ) and not (kind is int and isinstance(value, bool))
        if not matches:
            raise PluginError(
                f"plugin {self.plugin!r} reads {name!r} as {kind.__name__}, "
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
    """Every claimed key, once flat for Ansible and once scoped per plugin.

    Two shapes of one answer, because there are two consumers and they need
    opposite things. ``environment`` is what ``PlaybookExecutor`` copies into
    the subprocess, where a role reads its own name with ``lookup('env', ...)``
    and the flatness *is* the interface. ``configs`` is what a package reads on
    the controller, where seeing another capability's credential would be the
    thing this whole seam exists to prevent.
    """

    environment: Mapping[str, SecretStr]
    configs: Mapping[str, CapabilityConfig]

    def for_plugin(self, plugin: str) -> CapabilityConfig:
        """This plugin's configuration; empty if it declared no keys.

        Empty rather than an error, because declaring no keys is the normal
        case and a package should not have to know whether it is one. A
        mistyped plugin name is caught a line later instead: every read against
        the empty config names a key that config does not declare.
        """
        return self.configs.get(plugin) or CapabilityConfig(plugin, {})


def resolve_capability_environment(
    contributions: Iterable[ConfigurationContribution],
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> ResolvedCapabilityEnvironment:
    """Resolve claimed configuration, and refuse what cannot be worked with.

    Four refusals, and each one is here because the alternative is a failure
    much later that names nothing useful. A key without the ``BLITZE_`` prefix
    is a package claiming a name the controller never collects. A key claimed
    twice is two packages reading one value, either of which may be reconfigured
    for the other's sake. A configured key nobody claims is a typo, a setting
    left behind by a detached package, or a package that was never installed —
    and silently ignoring it is how an operator spends an afternoon on a
    credential that was reaching nothing.

    The fourth is the value itself: a declared-required key that is absent, a
    present one shorter than the length its capability declared usable, or a
    setting that cannot be read as the type it was declared with. All are
    ``ConfigurationError`` rather than ``PluginError``, and the distinction is
    the one the exception hierarchy already draws — nothing is wrong with the
    installed package, and the fix is a value the operator can change.

    Secrets and settings share one namespace deliberately. They arrive as one
    set of `BLITZE_*` names and an operator sets them the same way, so a
    capability claiming one name as both — or two capabilities disagreeing
    about which kind a name is — is the same collision as any other and is
    reported as one.

    Only *secrets* are forwarded to Ansible. A setting is resolved for the
    controller, and a role that needs the same value reads it from the desired
    state document or from its own role defaults, which is where non-secret
    fleet policy already lives.

    The two sources are kept apart on purpose. ``configured`` is the process
    environment and the controller's ``.env``, which is 0600 and uncommitted;
    ``from_file`` is ``blitzecdn.toml``, which is neither. A setting may come
    from either, the environment winning; a *secret* may come only from the
    first, so a package cannot document its signing key into a file an
    operator would commit. That rule used to be enforced by the TOML reader
    refusing every name it did not recognise — which also made every
    non-secret capability setting unconfigurable there.
    """
    owners: dict[str, str] = {}
    declared: dict[str, ConfigurationContribution] = {}
    secrets: set[str] = set()
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for name in _claimed_names(contribution):
            if not name.startswith("BLITZE_"):
                raise PluginError(
                    f"plugin {contribution.plugin!r} claims configuration name "
                    f"{name!r}, which must start with 'BLITZE_'"
                )
            if name in owners:
                raise PluginError(
                    f"configuration name {name!r} is claimed by both plugin "
                    f"{owners[name]!r} and plugin {contribution.plugin!r}"
                )
            owners[name] = contribution.plugin
        secrets.update(key.name for key in contribution.environment_keys)
        declared[contribution.plugin] = contribution
    unknown = sorted((set(configured) | set(from_file)) - owners.keys())
    if unknown:
        raise PluginError(
            "unknown capability configuration: "
            + ", ".join(unknown)
            + ". A name here is claimed by no installed capability: check the "
            "spelling, or whether the package that owned it was detached. In "
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
        plugin: resolve_plugin_configuration(
            contribution, configured, from_file, state_dir
        )
        for plugin, contribution in declared.items()
    }
    # Secrets only. A setting has a resolved value whether or not an operator
    # supplied one, so forwarding the staged strings would send Ansible the
    # subset that happened to be set rather than the answer.
    environment = {key: configured[key] for key in sorted(configured) if key in secrets}
    return ResolvedCapabilityEnvironment(environment=environment, configs=configs)


def _claimed_names(contribution: ConfigurationContribution) -> tuple[str, ...]:
    """Every `BLITZE_*` name one contribution claims, secrets and settings."""
    return tuple(key.name for key in contribution.environment_keys) + tuple(
        setting.name for setting in contribution.settings
    )


def resolve_plugin_configuration(
    contribution: ConfigurationContribution,
    configured: Mapping[str, SecretStr],
    from_file: Mapping[str, str],
    state_dir: Path,
) -> CapabilityConfig:
    """Resolve one capability's own configuration, and only its own.

    Public because one caller genuinely has no control plane to ask. Backup's
    commands are built from ``Settings`` alone and open no repository — that is
    the point of them, since a controller whose database will not open is
    exactly when a restore is wanted — so ``platform.capability_config`` does
    not exist on that path. The package calls this with the contribution it
    already declared, and gets the same object the composition root would have
    handed it.

    What it deliberately does not do is the cross-plugin half: collisions with
    other capabilities and names nobody claimed are questions about the whole
    installation, and they are answered once, at startup, by
    :func:`resolve_capability_environment`. This resolves values.
    """
    values = {
        key.name: configured.get(key.name, SecretStr(""))
        for key in sorted(contribution.environment_keys, key=lambda item: item.name)
    }
    for key in contribution.environment_keys:
        _check_value(contribution.plugin, key, values[key.name])
    settings = {
        setting.name: _resolve_setting(
            contribution.plugin,
            setting,
            # The environment wins over the file, which is the precedence every
            # other source in this control plane follows.
            configured.get(setting.name)
            or _optional_secret(from_file.get(setting.name)),
            state_dir,
        )
        for setting in sorted(contribution.settings, key=lambda item: item.name)
    }
    return CapabilityConfig(contribution.plugin, values, settings)


def _optional_secret(value: str | None) -> SecretStr | None:
    """Wrap a file-sourced value so both sources reach one reader."""
    return None if value is None else SecretStr(value)


def _resolve_setting(
    plugin: str, setting: CapabilitySetting, supplied: SecretStr | None, state_dir: Path
) -> SettingValue:
    """Read one configured value as the type its capability declared.

    An unsupplied setting is its default, which is what makes a setting
    different from a secret: there is always a value, so no package has to
    carry an "or the default" expression at every read.

    A supplied one is staged as ``SecretStr`` like everything else core cannot
    yet attribute — core does not learn which names are secrets until the
    plugins declare it, and treating an unattributed value as a secret until
    told otherwise is the only safe order. Unwrapping here is the moment its
    owner has said it is not one.
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
            f"{plugin!r} cannot use: {exc}"
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
    """Resolve a relative path setting against this controller's state.

    Only a path is touched. A capability names a location before it can know
    where this controller keeps its state, so a relative one is read the way
    core already read its own ``backup_dir``: under the state directory. An
    absolute one is taken as written, which is what an installed controller
    configures — ``/var/backups/blitzecdn`` rather than a directory inside the
    tree an uninstall removes.
    """
    if isinstance(value, Path) and not value.is_absolute():
        return state_dir / value
    return value


def _check_value(plugin: str, key: EnvironmentKey, value: SecretStr) -> None:
    """Hold one value to the two rules core can state without reading it."""
    secret = value.get_secret_value()
    detail = f" {key.summary}" if key.summary else ""
    if not secret:
        if key.required:
            raise ConfigurationError(
                f"the installed capability {plugin!r} requires {key.name}, which "
                f"is not set on this controller.{detail}"
            )
        return
    if key.minimum_bytes and len(secret.encode("utf-8")) < key.minimum_bytes:
        raise ConfigurationError(
            f"{key.name} is set to fewer than {key.minimum_bytes} bytes, which "
            f"the capability {plugin!r} declares unusable.{detail}"
        )


@dataclass(frozen=True, slots=True)
class ResolvedEdgeModule:
    """One dynamic module the installed capabilities need, and who asked."""

    plugin: str
    name: str
    objects: tuple[str, ...]
    build: bool
    probe: str


def resolve_edge_modules(
    contributions: Iterable[AnsibleContribution],
) -> tuple[ResolvedEdgeModule, ...]:
    """Compose the fleet's `load_module` set from what is installed.

    The companion to :func:`resolve_nginx_resources` for the one Nginx input
    that is not a fragment: `load_module` is a main-context directive, so the
    modules an edge loads are a single process-wide list, and core composes it
    the way it composes the role search path — ordered by plugin name so two
    controllers with the same packages resolve the same list, and by declared
    order within one plugin, which is the only ordering a capability owns.

    Two plugins declaring the *same* module is allowed when they declare it
    identically, and that is deliberate rather than lenient. njs is the case:
    it is one shared object in the base image and any number of capabilities
    may want it, and refusing the second would force one of them to declare a
    dependency on the other over a file neither of them owns. What is refused
    is the same name declared two different ways, which is not a capability
    asking for a module but two capabilities disagreeing about what it is —
    and Nginx would take whichever this happened to emit first.
    """
    resolved: list[ResolvedEdgeModule] = []
    owners: dict[str, ResolvedEdgeModule] = {}
    objects: dict[str, str] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for module in contribution.edge_modules:
            candidate = ResolvedEdgeModule(
                plugin=contribution.plugin,
                name=module.name,
                objects=tuple(module.objects),
                build=module.build,
                probe=module.probe,
            )
            existing = owners.get(module.name)
            if existing is not None:
                if _same_module(existing, candidate):
                    continue
                raise PluginError(
                    f"Nginx module {module.name!r} is declared differently by "
                    f"plugin {existing.plugin!r} and plugin "
                    f"{contribution.plugin!r}. One module is one shared object "
                    "set built one way; two descriptions of it means an edge "
                    "loads whichever was resolved first."
                )
            for shared_object in candidate.objects:
                if shared_object in objects:
                    raise PluginError(
                        f"the shared object {shared_object!r} is loaded by both "
                        f"module {objects[shared_object]!r} and module "
                        f"{module.name!r}; Nginx refuses a module loaded twice."
                    )
                objects[shared_object] = module.name
            owners[module.name] = candidate
            resolved.append(candidate)
    return tuple(resolved)


def _same_module(left: ResolvedEdgeModule, right: ResolvedEdgeModule) -> bool:
    """Whether two declarations describe the same module, ignoring who asked."""
    return (left.objects, left.build, left.probe) == (
        right.objects,
        right.build,
        right.probe,
    )


#: How core is named in a conflict message. Not a plugin name — nothing
#: registers under it — only the label the failure needs to name both sides.
_CORE = "the control plane"


def resolve_role_search_path(
    core_roles: Path, contributions: Iterable[AnsibleContribution]
) -> tuple[Path, ...]:
    """Core's roles directory, then each contributor's, deterministically.

    Ordering is core first and then contributions by plugin name, so the path a
    deployment runs against depends on *what is installed* and never on the
    order pluggy happened to register it in. Two edges converged from the same
    set of packages resolve every role identically.

    A role name that appears in two directories is refused with both owners
    named, rather than silently shadowed. Shadowing is the failure this exists
    to prevent: Ansible takes the first match, so a package shipping
    ``blitzecdn_nginx`` would replace the edge's configuration renderer and the
    deployment would succeed while converging something nobody wrote.

    A contributed directory that is not there is refused too. It means the
    package declared a role tree its wheel did not carry, and the alternative
    to failing here is a play that fails much later with "the role was not
    found", naming nothing that would lead anyone to the package.
    """
    ordered = sorted(contributions, key=lambda item: item.plugin)
    search: list[Path] = [core_roles]
    owners: dict[str, str] = dict.fromkeys(_role_names(core_roles), _CORE)
    for contribution in ordered:
        path = contribution.roles_path
        if not path.is_dir():
            raise PluginError(
                f"plugin {contribution.plugin!r} contributes the Ansible roles "
                f"directory {path}, which does not exist. Its distribution was "
                "built without its Ansible resources, or was installed in a way "
                "that did not carry them."
            )
        for role in _role_names(path):
            if role in owners:
                raise PluginError(
                    f"role {role!r} is shipped by both {owners[role]} and "
                    f"plugin {contribution.plugin!r}. Ansible resolves a role name "
                    "against the first directory that has it, so one of them "
                    "would silently replace the other; rename the role in the "
                    "package that added it."
                )
            owners[role] = f"plugin {contribution.plugin!r}"
        search.append(path)
    return tuple(search)


def resolve_edge_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """Which contributed roles core's edge play runs, in a fixed order.

    The companion to the search path, and separate from it for the same reason
    the two questions are separate: the search path says where a role *is*, and
    this says which ones the shared edge play *includes*. A package that ships
    a role only its own plays reach — a purge, a statistics collection —
    answers this with nothing and converges no edge on a deploy.
    """
    return _resolve_slot(contributions, "edge_roles")


def resolve_host_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The same question for the play's *host* slot, at the end of the run.

    A separate list rather than a flag on the names in one, because the two
    slots are two positions in a play and a role belongs to exactly one of
    them. Composed identically, and deliberately by the same private helper:
    the ordering rule, the "your wheel does not ship that" refusal and the
    determinism are properties of *a slot*, and a second copy of them would be
    a second place for them to drift.
    """
    return _resolve_slot(contributions, "host_roles")


def resolve_teardown_capability_roles(
    contributions: Iterable[AnsibleContribution],
) -> tuple[str, ...]:
    """The same question again, for the decommission play's slot.

    A third list rather than a reuse of either edge slot, because a
    decommission is a different play with a different guarantee: it runs on a
    host that is about to leave inventory, and after it there is no way back.
    A capability removes what it wrote here or it stays on the host forever.

    Composed by the same helper for the same reason as the other two, and
    ordered by plugin name like them — with one consequence worth naming.
    Removal order is not the reverse of convergence order and does not need to
    be: each role withdraws only what its own package wrote, and nothing in
    this slot may depend on another capability's files still being present.
    """
    return _resolve_slot(contributions, "teardown_roles")


def _resolve_slot(
    contributions: Iterable[AnsibleContribution], slot: str
) -> tuple[str, ...]:
    """Compose one of core's capability slots.

    Ordered by plugin name, like the search path, so a fleet converged from the
    same set of packages runs the same roles in the same order every time. This
    ordering has no dependency semantics: optional roles must depend only on
    established core prerequisites. Within one plugin the declared order is
    kept because that package alone owns those roles — which is how
    ``blitzecdn-hardening`` gets Fail2Ban after SSH without core knowing that
    either exists.

    A name the contributing package does not actually ship is refused here.
    Ansible would refuse it too, but much later — after the engine is
    installed, the image pulled and the play half-way through an edge — and it
    would name only the role, not the distribution that asked for it. In the
    decommission slot that lateness is worse still: the play would have already
    started taking the host apart.
    """
    roles: list[str] = []
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        requested: tuple[str, ...] = getattr(contribution, slot)
        if not requested:
            continue
        available = set(_role_names(contribution.roles_path))
        for role in requested:
            if role not in available:
                raise PluginError(
                    f"plugin {contribution.plugin!r} asks for the role {role!r} "
                    f"in the {slot!r} slot, which its own roles directory "
                    f"{contribution.roles_path} does not contain."
                )
            roles.append(role)
    return tuple(roles)


def _role_names(path: Path) -> Sequence[str]:
    """Every role directory directly under ``path``, sorted.

    A role is a directory, so a stray file is not one. Nothing here reads what
    is inside: whether a role is *valid* is Ansible's question and
    ansible-lint's, and answering it here would be a second, weaker copy of
    both. A directory that is not there contributes no names — a contributed
    one has already been refused by then, and core's is checked by
    ``Settings.validate_runtime`` where every other missing path is.
    """
    if not path.is_dir():
        return ()
    return sorted(entry.name for entry in path.iterdir() if entry.is_dir())
