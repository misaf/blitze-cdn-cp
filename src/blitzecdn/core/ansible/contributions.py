"""What the registered capabilities add to an Ansible run.

The composition root resolves these from the feature registry and hands the
finished value to the Ansible adapter, so ``core.ansible`` never reaches for
the registry. One object rather than seven parameters, because both
:class:`~blitzecdn.core.ansible.runner.AnsibleRunner` and
:class:`~blitzecdn.core.ansible.execution.PlaybookExecutor` carry it unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from blitzecdn.core.features.resolution import ResolvedEdgeModule, ResolvedNginxResource

__all__ = ["EdgeContributions"]


@dataclass(frozen=True, slots=True)
class EdgeContributions:
    """What the capabilities add to every run."""

    #: Core's roles directory, then every feature's, as
    #: :func:`~blitzecdn.core.features.resolution.resolve_role_search_path` orders them.
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
    #: The Nginx dynamic modules those fragments need. Not desired state, so a
    #: rollback never pins them.
    edge_modules: tuple[ResolvedEdgeModule, ...] = ()
    #: Declared secrets, forwarded only through Ansible's environment — never
    #: argv, never desired state.
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
        """Build one from the resolvers' output, freezing every sequence."""
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

    def variables(self) -> dict[str, Any]:
        """The capability contributions as extra-vars, never in a release's variables.

        The set of capabilities is not desired state; pinning it would make a rollback
        run a role a later version no longer ships. Nothing here is secret.
        """
        return {
            "blitzecdn_capability_roles": list(self.edge_roles),
            "blitzecdn_host_capability_roles": list(self.host_roles),
            "blitzecdn_edge_teardown_capability_roles": list(self.teardown_roles),
            "blitzecdn_nginx_modules": [
                {
                    "feature": module.feature,
                    "name": module.name,
                    "objects": list(module.objects),
                }
                for module in self.edge_modules
            ],
            "blitzecdn_nginx_resources": {
                context: [
                    {
                        "feature": resource.feature,
                        "name": resource.name,
                        "template": str(resource.template),
                    }
                    for resource in resources
                ]
                for context, resources in self.nginx_resources.items()
            },
        }
