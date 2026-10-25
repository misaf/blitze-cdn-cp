"""What to run, against which edges, with which variables."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.execution import PlaybookExecutor, PlaybookRun
from blitzecdn.core.ansible.hosts import resolve_limit, targeted_hosts
from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.ansible.variables import run_variables
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import AnsibleRun
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.ports.fleet import FleetRoster

__all__ = ["AnsibleRunner"]

#: A syntax check parses the play and executes nothing, so it is bounded by how
#: long parsing can reasonably take rather than by the deployment timeout.
_PARSE_TIMEOUT = 120


class AnsibleRunner:
    """Runs Ansible against the fleet the control plane records.

    ``fleet`` is the same rows the inventory plugin reads, so a ``--limit`` expands
    against exactly what Ansible will be given. Capabilities each declare the
    slice they need as their own port; their own plays go through
    :meth:`run_playbook`.
    """

    def __init__(
        self,
        settings: Settings,
        fleet: FleetRoster,
        contributions: EdgeContributions | None = None,
    ) -> None:
        self._settings = settings
        self._fleet = fleet
        # Core's roles alone, and no capability roles, when nobody says
        # otherwise. The composition root resolves what is installed and passes
        # it; a test that only runs core's plays needs no registry to build a
        # runner.
        if contributions is None:
            contributions = EdgeContributions(
                roles_path=(settings.ansible_dir / "roles",)
            )
        self._executor = PlaybookExecutor(settings, contributions)

    def lock(self) -> DeploymentLock:
        return DeploymentLock(self._settings.deployment_lock_path)

    def validate(self, variables: Path) -> AnsibleRun:
        """Parse the playbook against ``variables``, changing nothing.

        ``--syntax-check`` reports no host events, so ``AnsibleRun.reported`` is
        false. The caller supplies the path: ``generated_vars_path`` belongs to
        whichever deploy holds the lock.
        """
        self._validate_paths()
        return self._executor.execute(
            PlaybookRun(
                playbook=self._settings.playbook_path,
                variables=variables,
                limit=self._limit(None),
                timeout=_PARSE_TIMEOUT,
                syntax_check=True,
            )
        )

    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun:
        self._validate_paths()
        limit = self._limit(host_limit)
        return self._executor.execute(
            PlaybookRun(
                playbook=self._settings.playbook_path,
                variables=self._settings.generated_vars_path,
                limit=limit,
                timeout=self._settings.deployment_timeout_seconds,
                check=check,
                targeted=targeted_hosts(self._fleet, limit),
                tags=tags,
            )
        )

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        """Strip BlitzeCDN configuration and TLS material from one edge.

        ``host_limit`` is required: an absent limit means every edge, which for a
        teardown would empty the fleet.
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
        """Run one capability play with the caller's variables, staged and time-limited.

        Takes no deployment lock: these are operations that write no desired state,
        and they are most needed exactly when a deploy is running.
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
            PlaybookRun(
                playbook=playbook,
                variables=variables,
                limit=limit,
                timeout=self._settings.deployment_timeout_seconds,
                targeted=targeted_hosts(self._fleet, limit),
            )
        )

    def _limit(self, host_limit: str | None) -> str:
        return resolve_limit(self._fleet, host_limit)

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
