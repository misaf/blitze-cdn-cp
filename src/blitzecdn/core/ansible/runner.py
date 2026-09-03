"""What to run, against which edges, with which variables."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import SecretStr

from blitzecdn.capabilities.edges.ports import EdgeStore
from blitzecdn.core.ansible.execution import PlaybookExecutor
from blitzecdn.core.ansible.hosts import resolve_limit, targeted_hosts
from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.ansible.variables import run_variables
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.plugins.resolution import ResolvedEdgeModule, ResolvedNginxResource
from blitzecdn.core.runs import AnsibleRun

__all__ = ["AnsibleRunner"]

#: A syntax check parses the play and executes nothing, so it is bounded by how
#: long parsing can reasonably take rather than by the deployment timeout.
_PARSE_TIMEOUT = 120


class AnsibleRunner:
    """Runs Ansible against the fleet the control plane records.

    ``edges`` is the same store the ``blitzecdn`` inventory plugin reads. It is
    injected rather than opened here because this needs it for one thing only —
    expanding a ``--limit`` into explicit host names — and because that
    expansion must be answered from the identical rows Ansible is about to be
    given. Reading a separate copy is precisely the drift that removing the
    static inventory file was meant to end.

    One adapter, several playbooks. Each capability declares the slice of this it
    actually needs as its own port — ``DeploymentRunner``, ``EdgeRunner``,
    ``DeploymentLocker`` — and the composition root is the only place that
    knows one object satisfies all of them. An installed capability's own
    operations are not among them: ``blitzecdn-cache``'s purge and
    ``blitzecdn-origins``' probe reach the fleet through the generic
    ``run_playbook`` below, so this class carries no method for a play that
    might not be installed.
    """

    def __init__(
        self,
        settings: Settings,
        edges: EdgeStore,
        roles_path: Sequence[Path] | None = None,
        capability_roles: Sequence[str] = (),
        host_capability_roles: Sequence[str] = (),
        teardown_capability_roles: Sequence[str] = (),
        nginx_resources: Mapping[str, Sequence[ResolvedNginxResource]] | None = None,
        edge_modules: Sequence[ResolvedEdgeModule] = (),
        capability_environment: Mapping[str, SecretStr] | None = None,
    ) -> None:
        self._settings = settings
        self._edges = edges
        # Core's roles alone, and no capability roles, when nobody says
        # otherwise. The composition root resolves both from the installed
        # plugins and passes them; a test that only runs core's plays needs
        # neither the registry nor a plugin manager to build a runner.
        self._executor = PlaybookExecutor(
            settings,
            roles_path if roles_path is not None else (settings.ansible_dir / "roles",),
            capability_roles=capability_roles,
            host_capability_roles=host_capability_roles,
            teardown_capability_roles=teardown_capability_roles,
            nginx_resources=nginx_resources,
            edge_modules=edge_modules,
            capability_environment=capability_environment,
        )

    def lock(self) -> DeploymentLock:
        return DeploymentLock(self._settings.deployment_lock_path)

    def validate(self, variables: Path) -> AnsibleRun:
        """Parse the playbook against ``variables``, changing nothing.

        The only run whose answer is the return code alone: ``--syntax-check``
        executes no play, so there is no host event to report.
        ``AnsibleRun.reported`` is false here by design, and the log file holds
        whatever Ansible said about the parse failure.

        The caller supplies the path rather than this reaching for
        ``generated_vars_path``: that file belongs to whichever deploy currently
        holds the lock, and validation must not write over it.
        """
        self._validate_paths()
        return self._executor.execute(
            playbook=self._settings.playbook_path,
            variables=variables,
            limit=self._limit(None),
            timeout=_PARSE_TIMEOUT,
            syntax_check=True,
        )

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun:
        self._validate_paths()
        limit = self._limit(host_limit)
        return self._executor.execute(
            playbook=self._settings.playbook_path,
            variables=self._settings.generated_vars_path,
            limit=limit,
            timeout=self._settings.deployment_timeout_seconds,
            check=check,
            targeted=targeted_hosts(self._edges, limit),
        )

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        """Strip BlitzeCDN configuration and TLS material from one edge.

        ``host_limit`` is required and never defaulted to the whole group: the
        other run methods treat an absent limit as "every edge", which for a
        teardown would empty the fleet. The caller names the host it is
        removing, and the playbook is fail-closed so a partial teardown keeps
        the inventory entry rather than stranding keys on a forgotten host.
        """
        with run_variables(self._settings.run_dir, "decommission", {}) as variables:
            return self._playbook_run(
                self._settings.decommission_playbook_path, variables, host_limit
            )

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Run one named play with variables the caller supplies.

        The primitive an installed capability reaches for. Purging a cache and
        collecting statistics were methods on this class while `cache` was a
        package inside this distribution, which meant core knew what a
        `PurgeEntry` was — a capability's domain type in the adapter every other
        capability also uses. They are the cache package's business now, and what
        core still owns is the part that is genuinely its own: which hosts a
        limit expands to, where the variables file is staged, and the timeout.

        Deliberately does not take the deployment lock. Everything reached this
        way is an *operation* rather than a convergence — it writes no desired
        state — and the moment an operation is most needed is the moment a
        deploy is most likely to already be running.
        """
        with run_variables(self._settings.run_dir, name, dict(variables)) as staged:
            return self._playbook_run(playbook, staged, host_limit)

    # -- Command construction ------------------------------------------

    def _playbook_run(
        self, playbook: Path, variables: Path, host_limit: str | None
    ) -> AnsibleRun:
        self._validate_paths()
        if not playbook.is_file():
            raise ConfigurationError(f"playbook does not exist: {playbook}")
        limit = self._limit(host_limit)
        return self._executor.execute(
            playbook=playbook,
            variables=variables,
            limit=limit,
            timeout=self._settings.deployment_timeout_seconds,
            targeted=targeted_hosts(self._edges, limit),
        )

    def _limit(self, host_limit: str | None) -> str:
        return resolve_limit(self._edges, host_limit)

    def _validate_paths(self) -> None:
        errors = self._settings.validate_runtime()
        if errors:
            raise ConfigurationError("; ".join(errors))
        executable = self._settings.ansible_playbook
        if "/" in executable:
            if not Path(executable).is_file():
                raise ConfigurationError(
                    f"ansible-playbook does not exist: {executable}"
                )
        elif shutil.which(executable) is None:
            raise ConfigurationError(
                f"ansible-playbook is not available on PATH: {executable}"
            )
