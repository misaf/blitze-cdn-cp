"""One invocation: launch it, keep its output, and say what it did.

Everything about *how* Ansible is run lives here — the environment, the
artifact tree, the operator log and its retention, and the mapping from
Runner's result to a :class:`~blitzecdn.core.runs.AnsibleRun`. What to run and
against which edges is :mod:`blitzecdn.core.ansible.runner`'s decision; by the
time it reaches this module the playbook, the variables file and the resolved
limit are all settled.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import ansible_runner  # type: ignore[import-untyped]
from ansible_runner import (
    exceptions as runner_exceptions,
)
from pydantic import SecretStr

from blitzecdn.core.ansible.events import RunnerEvents
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.core.nginx import ResolvedNginxResource
from blitzecdn.core.runs import AnsibleRun, HostRun, RunStatus

__all__ = ["PlaybookExecutor"]

#: Exit code recorded for a run killed at its timeout, matching the shell
#: convention for a process ended by a signal after a deadline.
_TIMEOUT_RETURN_CODE = 124


class PlaybookExecutor:
    """Runs one playbook and returns the structured account of what happened."""

    def __init__(
        self,
        settings: Settings,
        roles_path: Sequence[Path],
        capability_roles: Sequence[str] = (),
        host_capability_roles: Sequence[str] = (),
        teardown_capability_roles: Sequence[str] = (),
        nginx_resources: Mapping[str, Sequence[ResolvedNginxResource]] | None = None,
        capability_environment: Mapping[str, SecretStr] | None = None,
    ) -> None:
        self._settings = settings
        #: Where Ansible resolves a role name, composed by
        #: :func:`blitzecdn.core.ansible.roles.resolve_role_search_path` from
        #: core's roles and the installed plugins'. Passed in rather than read
        #: from configuration because it is a fact about what is *installed*,
        #: which only the composition root knows.
        self._roles_path = tuple(roles_path)
        #: Which contributed roles the edge play runs, from the same source.
        self._capability_roles = tuple(capability_roles)
        #: And which it runs in its host slot, after the edge is serving.
        self._host_capability_roles = tuple(host_capability_roles)
        #: And which the decommission play runs before core's teardown, to
        #: take a capability's own files off a host that is leaving.
        self._teardown_capability_roles = tuple(teardown_capability_roles)
        self._nginx_resources = {
            context: tuple(resources)
            for context, resources in (nginx_resources or {}).items()
        }
        self._capability_environment = dict(capability_environment or {})

    def execute(
        self,
        *,
        playbook: Path,
        variables: Path,
        limit: str,
        timeout: int,
        check: bool = False,
        syntax_check: bool = False,
        targeted: tuple[str, ...] = (),
    ) -> AnsibleRun:
        run_id = uuid4().hex
        started_at = datetime.now(UTC)
        log_path = self._settings.log_dir / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        environment = self._environment()
        events = RunnerEvents()
        control_root = self._settings.state_dir / "ansible-control"
        artifact_root = self._settings.state_dir / "ansible-runner"
        control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact_path = artifact_root / run_id
        try:
            with tempfile.TemporaryDirectory(dir=control_root) as control_path:
                environment["ANSIBLE_SSH_CONTROL_PATH_DIR"] = control_path
                result = self._run_ansible(
                    run_id=run_id,
                    artifact_root=artifact_root,
                    playbook=playbook,
                    variables=variables,
                    limit=limit,
                    environment=environment,
                    timeout=timeout,
                    check=check,
                    syntax_check=syntax_check,
                    event_handler=events,
                )
        except BaseException:
            # Runner can create its artifact tree before failing to launch or
            # before returning a result. Preserve its output, but never leave
            # the potentially large event spool behind on an exception path.
            self._keep_runner_output(artifact_path, log_path)
            shutil.rmtree(artifact_path, ignore_errors=True)
            self._prune_logs()
            raise
        self._keep_runner_output(artifact_path, log_path)
        shutil.rmtree(artifact_path, ignore_errors=True)

        status = (
            RunStatus.TIMED_OUT
            if result.status == "timeout"
            else RunStatus.SUCCEEDED
            if result.rc == 0
            else RunStatus.FAILED
        )
        return_code = (
            _TIMEOUT_RETURN_CODE if status is RunStatus.TIMED_OUT else result.rc
        )
        hosts = events.hosts()
        self._prune_logs()
        return AnsibleRun(
            id=run_id,
            playbook=playbook.name,
            status=status,
            return_code=return_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            hosts=hosts,
            targeted=targeted,
            log_path=str(log_path),
            error=self._unreported_detail(status, hosts, log_path),
        )

    def _run_ansible(
        self,
        *,
        run_id: str,
        artifact_root: Path,
        playbook: Path,
        variables: Path,
        limit: str,
        environment: dict[str, str],
        timeout: int,
        check: bool,
        syntax_check: bool,
        event_handler: RunnerEvents,
    ) -> Any:
        """Execute through Ansible Runner without exposing it above this adapter."""
        options = [
            "--extra-vars",
            f"@{variables}",
            # What is installed, on the command line rather than in the
            # variables file. The variables file for a deployment *is* the
            # desired-state snapshot — the document a rollback converges months
            # later — and the set of installed packages is not desired state:
            # pinning it would make a rollback try to run a capability role
            # that has since been detached. Every run gets the list as it is
            # now, and a play that has no use for it ignores it.
            #
            # Nothing secret goes here. These are role names, which are already
            # readable in every wheel on the controller; credentials reach
            # Ansible through the environment precisely so they stay out of the
            # process table.
            "--extra-vars",
            json.dumps(
                {
                    "blitzecdn_capability_roles": list(self._capability_roles),
                    "blitzecdn_host_capability_roles": list(
                        self._host_capability_roles
                    ),
                    "blitzecdn_teardown_capability_roles": list(
                        self._teardown_capability_roles
                    ),
                    "blitzecdn_nginx_resources": {
                        context: [
                            {
                                "plugin": resource.plugin,
                                "name": resource.name,
                                "template": str(resource.template),
                            }
                            for resource in resources
                        ]
                        for context, resources in self._nginx_resources.items()
                    },
                }
            ),
        ]
        if syntax_check:
            options.append("--syntax-check")
        elif check:
            options.extend(("--check", "--diff"))
        # Supplying a custom binary puts Runner in raw execution mode, where it
        # deliberately does not append its `playbook` parameter. Keep the
        # configured executable support and make the playbook explicit.
        options.append(str(playbook))
        try:
            return ansible_runner.run(
                private_data_dir=str(self._settings.state_dir),
                project_dir=str(self._settings.ansible_dir),
                artifact_dir=str(artifact_root),
                ident=run_id,
                inventory=str(self._settings.inventory_path),
                limit=limit,
                binary=self._settings.ansible_playbook,
                cmdline=shlex.join(options),
                envvars=environment,
                settings={"runner_mode": "subprocess"},
                timeout=timeout,
                quiet=True,
                suppress_env_files=True,
                rotate_artifacts=0,
                event_handler=event_handler,
            )
        except (OSError, runner_exceptions.AnsibleRunnerException) as exc:
            raise ExecutionError(f"unable to execute Ansible: {exc}") from exc

    @staticmethod
    def _keep_runner_output(artifact_path: Path, log_path: Path) -> None:
        """Move Runner's combined stdout into the stable operator log path."""
        stdout = artifact_path / "stdout"
        try:
            stdout.replace(log_path)
        except OSError:
            # Runner may fail before it creates stdout. Preserve the invariant
            # that every attempted run still has a log path to inspect.
            log_path.touch(mode=0o600, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["ANSIBLE_CONFIG"] = str(self._settings.ansible_dir / "ansible.cfg")
        # Overrides `roles_path` in ansible.cfg, which can only name directories
        # relative to itself and therefore cannot reach a role that lives inside
        # an installed wheel. Absolute, in the order the composition root
        # resolved, and set even when it holds only core's directory so a run
        # never depends on the cfg value and the env value agreeing.
        environment["ANSIBLE_ROLES_PATH"] = os.pathsep.join(
            str(path) for path in self._roles_path
        )
        environment["ANSIBLE_LOCAL_TEMP"] = str(
            self._settings.state_dir / "ansible-local"
        )
        Path(environment["ANSIBLE_LOCAL_TEMP"]).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        # Where the `blitzecdn` inventory plugin reads the fleet from. Absolute,
        # because Ansible runs with cwd set to `ansible_dir` and the configured
        # path may well be relative to the project root instead. This is the
        # whole of the coupling between the control plane and its inventory:
        # one environment variable naming one database.
        environment["BLITZE_DATABASE_PATH"] = str(
            self._settings.database_path.resolve()
        )
        # Optional capability configuration after plugin ownership has been
        # resolved. The composition root rejects unclaimed and multiply
        # claimed keys, so this contains only names explicitly owned by one
        # installed package.
        #
        # The environment, and not `--extra-vars`, because these are usually
        # credentials: an extra-var is in the process table for every user on
        # the controller, and in any file the run writes.
        for name, value in self._capability_environment.items():
            environment[name] = value.get_secret_value()
        return environment

    def _prune_logs(self) -> None:
        """Keep the newest ``run_log_retention`` logs and drop the rest.

        One file per invocation, and the drift timer alone produces one on
        every firing, so this directory only ever grows. Pruning here rather
        than on a timer of its own means retention cannot silently stop being
        applied because a unit was never installed.
        """
        try:
            logs = sorted(
                self._settings.log_dir.glob("*.log"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in logs[self._settings.run_log_retention :]:
            # A log another process is reading, or has already removed, is not
            # worth failing a completed run over.
            with suppress(OSError):
                stale.unlink()

    @staticmethod
    def _unreported_detail(
        status: RunStatus, hosts: tuple[HostRun, ...], log_path: Path
    ) -> str | None:
        """Explain a run that finished badly without saying which host failed.

        Ansible refusing an inventory, a playbook that will not parse, a
        connection plugin that died — all end the process before any host is
        reported. Point at the log rather than leaving the caller with a bare
        exit code.
        """
        if hosts or status is RunStatus.SUCCEEDED:
            return None
        return f"Ansible reported no per-host result. The full output is at {log_path}."
