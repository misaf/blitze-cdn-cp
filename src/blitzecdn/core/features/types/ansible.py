"""What a capability puts on an edge host.

Every member is global — the role search path, a play slot, the one
``load_module`` list an Nginx process has — so core composes them.
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
    """One Nginx dynamic module a capability needs on the edge.

    One declaration feeds both the `load_module` list each release renders and
    the build arguments `blitzecdn edge image spec` emits, so the image and the
    configuration agree. `name` is pkg-oss's name for it; `objects` are the
    shared objects to load, in order; `build` is false when the official base
    image already carries it; `probe` is one directive it registers, for the
    image's build-time probe.
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
    """The Ansible roles one feature ships, and the play slot each runs in.

    Core adds `roles_path` to Ansible's search path and runs the named roles by
    position in its own plays: `edge_roles` before the configuration is
    rendered, `host_roles` after the edge is serving, `teardown_roles` in the
    decommission play. Every name must exist under `roles_path`. A playbook is
    not global, so the feature passes its own to ``PlaybookRunner.run_playbook``.
    """

    feature: str
    roles_path: Path
    edge_roles: tuple[str, ...] = ()
    #: For a capability that configures the host rather than the runtime.
    host_roles: tuple[str, ...] = ()
    #: Run before ``blitzecdn_edge_teardown``, for anything this capability
    #: wrote outside the trees core already removes.
    teardown_roles: tuple[str, ...] = ()
    edge_modules: tuple[EdgeModule, ...] = ()
