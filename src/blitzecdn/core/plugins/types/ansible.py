"""What an installed capability puts on an edge host.

Answered by ``blitzecdn_ansible_contributions``. Every member describes
something global — a role search path, a play slot, the one ``load_module``
list an Nginx process has — which is why core has to compose it rather than
let each package apply its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AnsibleContribution",
    "EdgeModule",
]


@dataclass(frozen=True, slots=True)
class EdgeModule:
    """One Nginx dynamic module an installed capability needs on the edge.

    A dynamic module has two halves, and they are answered in two different
    places. It has to be *in the image* — built against that exact Nginx ABI,
    or already shipped by the official base — and it has to be *loaded* by the
    running configuration, which is one `load_module` list the whole process
    shares. Written into the edge image's own build context, both halves would
    make that build a third register of which capabilities exist — and the only
    one that keeps naming a capability after its distribution is detached,
    because an image is built once and pinned by digest: an edge with no
    `blitzecdn-geoip` would still load the GeoIP2 module.

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
    ``blitzecdn_edge_teardown``, so that role's clean-host assertion is the last
    word on the whole decommission rather than a verdict passed before half
    the removal happened.

    A capability declares any of them, or none. Core enforces the ordering by
    position in its own plays and never learns a role's name.

    `plugin` is here for the failure message and for ordering. Two packages
    shipping a role of the same name is a conflict that must be reported with
    both names rather than resolved by whichever happened to register last.

    Five members, every one of which describes a file on an edge. What a
    capability asks an operator to configure is not among them, and the line is
    the contract's name: a secret is forwarded into Ansible's subprocess
    environment and so has a reason to be near this, but an interval a
    scheduler reads on the controller never touches Ansible at all. Both belong
    to :class:`ConfigurationContribution` instead.
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
    #: decommission play runs before ``blitzecdn_edge_teardown``, to remove what
    #: this capability put on the host. Empty for a capability that writes
    #: nothing outside the trees core already removes — the data directory,
    #: the state tree, and any systemd unit matching the managed prefix.
    teardown_roles: tuple[str, ...] = ()
    #: Nginx dynamic modules this capability's configuration needs loaded.
    #: Global for the same reason the role lists are: `load_module` is a
    #: main-context directive and there is one list per Nginx process, so core
    #: composes it. Empty for a capability that adds no module — most of them.
    edge_modules: tuple[EdgeModule, ...] = ()
