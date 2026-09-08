"""What an installed capability puts on an edge host.

Answered by ``blitzecdn_ansible_contributions``. Every member describes
something global — a role search path, a play slot, the one ``load_module``
list an Nginx process has — which is why core has to compose it rather than
let each package apply its own.

The three play slots, why their order is core's rather than a preference, and
why a module is declared here instead of built into the image, are
``docs/decisions/0003-what-a-capability-puts-on-an-edge.md``.
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

    Declared rather than written into the image's build context, so that the
    two halves — the module being *in* the image and being *loaded* by the
    running configuration — have one answer. Core composes the resolved set
    into the `load_module` list `blitzecdn_nginx` renders, and `blitzecdn edge
    image spec` emits the same set as build arguments.

    `name` is the module as Nginx's own pkg-oss tooling names it, because that
    name is what the build takes. `objects` are the shared objects to load, in
    order; core supplies the directory they live in. `build` says whether the
    image must compile it or the official base already carries it. `probe` is
    one directive the module registers, for the image's build-time probe.

    See ``docs/decisions/0003-what-a-capability-puts-on-an-edge.md``.
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

    Every member describes something *global* — the search path, one ordered
    list per play slot, the one `load_module` list an Nginx process has — which
    is why core composes them. A playbook is not global and is deliberately
    absent: the package that owns a play passes its own path to
    ``PlaybookRunner.run_playbook``.

    Three slots, because there are three answers to *when*: `edge_roles` before
    the configuration tree is rendered and validated, `host_roles` after the
    edge is serving, `teardown_roles` in the decommission play. Core enforces
    the ordering by position in its own plays and never learns a role's name.
    Which slot a capability needs, and why the order is not a preference, is
    ``docs/decisions/0003-what-a-capability-puts-on-an-edge.md``.

    Every role list names roles this contribution's own `roles_path` ships —
    core refuses a name that is not there rather than letting the play fail much
    later. `plugin` is here for the failure message: two packages shipping a
    role of the same name is a conflict reported with both names.
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
