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
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, PluginError
from blitzecdn.core.plugins.types import (
    AnsibleContribution,
    EnvironmentKey,
    NginxContribution,
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

    def secret(self, name: str) -> SecretStr:
        """This key's value, empty when the controller has not set it."""
        if name not in self.values:
            raise PluginError(
                f"plugin {self.plugin!r} reads the environment key {name!r}, "
                "which it does not declare in its Ansible contribution. A key "
                "is claimed once, in `environment_keys`, and read by that name."
            )
        return self.values[name]

    def is_set(self, name: str) -> bool:
        """Whether the controller supplied a value for this declared key."""
        return bool(self.secret(name).get_secret_value())


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
    contributions: Iterable[AnsibleContribution],
    configured: Mapping[str, SecretStr],
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

    The fourth is the value itself: a declared-required key that is absent, or
    a present one shorter than the length its capability declared usable. Both
    are ``ConfigurationError`` rather than ``PluginError``, and the distinction
    is the one the exception hierarchy already draws — nothing is wrong with
    the installed package, and the fix is a value the operator can change.
    """
    owners: dict[str, str] = {}
    declared: dict[str, list[EnvironmentKey]] = {}
    for contribution in sorted(contributions, key=lambda item: item.plugin):
        for key in contribution.environment_keys:
            if not key.name.startswith("BLITZE_"):
                raise PluginError(
                    f"plugin {contribution.plugin!r} claims environment key "
                    f"{key.name!r}, which must start with 'BLITZE_'"
                )
            if key.name in owners:
                raise PluginError(
                    f"environment key {key.name!r} is claimed by both plugin "
                    f"{owners[key.name]!r} and plugin {contribution.plugin!r}"
                )
            owners[key.name] = contribution.plugin
            declared.setdefault(contribution.plugin, []).append(key)
    unknown = sorted(set(configured) - owners.keys())
    if unknown:
        raise PluginError(
            "unknown capability environment configuration: " + ", ".join(unknown)
        )
    configs: dict[str, CapabilityConfig] = {}
    for plugin, keys in declared.items():
        values: dict[str, SecretStr] = {}
        for key in sorted(keys, key=lambda item: item.name):
            value = configured.get(key.name, SecretStr(""))
            _check_value(plugin, key, value)
            values[key.name] = value
        configs[plugin] = CapabilityConfig(plugin, values)
    environment = {key: configured[key] for key in sorted(configured) if key in owners}
    return ResolvedCapabilityEnvironment(environment=environment, configs=configs)


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
