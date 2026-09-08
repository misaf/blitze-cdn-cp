"""What the installed capabilities add to an Ansible run.

Seven values that always travel together and always come from one place. The
composition root resolves them from the plugin registry — it is the only thing
that knows what is installed — and the Ansible adapter is handed the finished
answer.

One object rather than seven parameters, because they are one fact: *this
installation*. Passed separately they have to be listed on
:class:`~blitzecdn.core.ansible.runner.AnsibleRunner` and again on
:class:`~blitzecdn.core.ansible.execution.PlaybookExecutor`, which forwards
every one of them unchanged — so adding an eighth kind of contribution means
editing two signatures and a call site to carry a value neither class reads.

Deliberately not resolved here. Each field is the output of a
``blitzecdn.core.plugins.resolution`` function that already refuses what cannot
work — a role two packages both ship, a module no capability declared — and
doing it here would make ``core.ansible`` reach for the registry rather than be
handed what it needs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from blitzecdn.core.plugins.resolution import ResolvedEdgeModule, ResolvedNginxResource

__all__ = ["EdgeContributions"]


@dataclass(frozen=True, slots=True)
class EdgeContributions:
    """One installation's answer to "what do the installed capabilities add?"."""

    #: Where Ansible resolves a role name: core's roles and every installed
    #: plugin's, in the order
    #: :func:`~blitzecdn.core.plugins.resolution.resolve_role_search_path`
    #: settled. One process-wide list, which is why core composes it.
    roles_path: tuple[Path, ...] = ()
    #: Contributed roles core's edge play runs before the configuration tree is
    #: rendered and validated.
    edge_roles: tuple[str, ...] = ()
    #: And the ones it runs in its host slot, after the edge is serving.
    host_roles: tuple[str, ...] = ()
    #: And the ones the decommission play runs before core's own teardown.
    teardown_roles: tuple[str, ...] = ()
    #: Nginx template fragments, by the context they are rendered into.
    nginx_resources: Mapping[str, tuple[ResolvedNginxResource, ...]] = field(
        default_factory=dict
    )
    #: The Nginx dynamic modules those fragments need loaded. Not desired
    #: state: it is what is *installed*, so it never enters the snapshot a
    #: rollback converges.
    edge_modules: tuple[ResolvedEdgeModule, ...] = ()
    #: The secrets declared by installed capabilities, forwarded into Ansible's
    #: subprocess environment and nowhere else — never argv, never desired
    #: state. See ``docs/decisions/0002-capability-configuration-ownership.md``.
    environment: Mapping[str, SecretStr] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        *,
        roles_path: Sequence[Path] = (),
        edge_roles: Sequence[str] = (),
        host_roles: Sequence[str] = (),
        teardown_roles: Sequence[str] = (),
        nginx_resources: Mapping[str, Sequence[ResolvedNginxResource]] | None = None,
        edge_modules: Sequence[ResolvedEdgeModule] = (),
        environment: Mapping[str, SecretStr] | None = None,
    ) -> EdgeContributions:
        """Build one from whatever sequence types the resolvers returned.

        The resolvers answer with sequences and mappings of sequences; this is
        the one place that freezes them, so no caller has to remember to.
        """
        return cls(
            roles_path=tuple(roles_path),
            edge_roles=tuple(edge_roles),
            host_roles=tuple(host_roles),
            teardown_roles=tuple(teardown_roles),
            nginx_resources={
                context: tuple(resources)
                for context, resources in (nginx_resources or {}).items()
            },
            edge_modules=tuple(edge_modules),
            environment=dict(environment or {}),
        )
