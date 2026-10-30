"""One invocation: launch it, keep its output, and say what it did.

How Ansible runs — environment, artifact tree, operator log and retention, and
the mapping to :class:`~blitzecdn.core.domain.runs.AnsibleRun`. What runs and
against which edges is settled by :mod:`blitzecdn.core.ansible.runner`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import ansible_runner  # type: ignore[import-untyped]
from ansible_runner import (
    exceptions as runner_exceptions,
)

from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.events import RunnerEvents
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import AnsibleRun, HostRun, RunStatus
from blitzecdn.core.exceptions import ExecutionError

__all__ = ["PlaybookExecutor", "PlaybookRun"]


@dataclass(frozen=True, slots=True)
class PlaybookRun:
    """One play to run, and the bounds to run it under."""

    playbook: Path
    variables: Path
    limit: str
    timeout: int
    #: Ansible's `--check --diff`: report what would change, change nothing.
    check: bool = False
    #: `--syntax-check`: parse and execute nothing. Wins over `check`.
    syntax_check: bool = False
    #: The hosts `limit` expands to, recorded on the result so a partial
    #: rollout says which edges it reached.
    targeted: tuple[str, ...] = ()
    #: `--tags`: which slices of the play to run; empty runs all of it. The
    #: play's preparation (engine, directories, pinned image, serve probe) is
    #: tagged `always`, so a tag no role declares stages without activating.
    tags: tuple[str, ...] = ()


#: Exit code recorded for a run killed at its timeout, matching the shell
#: convention for a process ended by a signal after a deadline.
_TIMEOUT_RETURN_CODE = 124


class PlaybookExecutor:
    """Runs one playbook and returns the structured account of what happened."""

    def __init__(
        self, settings: Settings, contributions: EdgeContributions | None = None
    ) -> None:
        self._settings = settings
        #: Resolved by the composition root; see `EdgeContributions`.
        self._contributions = contributions or EdgeContributions()

    def execute(self, request: PlaybookRun) -> AnsibleRun:
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
                    request,
                    run_id=run_id,
                    artifact_root=artifact_root,
                    environment=environment,
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
            playbook=request.playbook.name,
            status=status,
            return_code=return_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            hosts=hosts,
            targeted=request.targeted,
            log_path=str(log_path),
            error=self._unreported_detail(status, hosts, log_path),
        )

    def _run_ansible(
        self,
        request: PlaybookRun,
        *,
        run_id: str,
        artifact_root: Path,
        environment: dict[str, str],
        event_handler: RunnerEvents,
    ) -> Any:
        """Execute through Ansible Runner without exposing it above this adapter."""
        options = [
            "--extra-vars",
            f"@{request.variables}",
            # What is installed, on the command line rather than in the
            # variables file; see `EdgeContributions.variables`. Credentials
            # reach Ansible through the environment so they stay out of the
            # process table.
            "--extra-vars",
            json.dumps(self._contributions.variables()),
        ]
        if request.tags:
            options.extend(("--tags", ",".join(request.tags)))
        if request.syntax_check:
            options.append("--syntax-check")
        elif request.check:
            options.extend(("--check", "--diff"))
        # Supplying a custom binary puts Runner in raw execution mode, where it
        # deliberately does not append its `playbook` parameter. Keep the
        # configured executable support and make the playbook explicit.
        options.append(str(request.playbook))
        try:
            return ansible_runner.run(
                private_data_dir=str(self._settings.state_dir),
                project_dir=str(self._settings.ansible_dir),
                artifact_dir=str(artifact_root),
                ident=run_id,
                inventory=str(self._settings.inventory_path),
                limit=request.limit,
                binary=self._settings.ansible_playbook,
                cmdline=shlex.join(options),
                envvars=environment,
                settings={"runner_mode": "subprocess"},
                timeout=request.timeout,
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
        # The controller's environment minus every `BLITZE_*` name, so no role
        # can `lookup('env')` the operators' API keys. What a play needs is
        # added back by name below: the database path and declared secrets.
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("BLITZE_")
        }
        environment["ANSIBLE_CONFIG"] = str(self._settings.ansible_dir / "ansible.cfg")
        # The installed controller mounts its SSH configuration under state;
        # checkouts normally use the operator's SSH agent and user config.
        # Common args add the optional config without replacing ansible.cfg's
        # authentication and connection options. An explicit operator override
        # keeps precedence, as it does for the other SSH environment settings.
        ssh_config = self._settings.state_dir / ".ssh" / "config"
        if ssh_config.is_file():
            environment.setdefault(
                "ANSIBLE_SSH_COMMON_ARGS", shlex.join(("-F", str(ssh_config)))
            )
        # Overrides ansible.cfg's `roles_path`, which can only name directories
        # relative to itself and so cannot reach roles inside the package.
        environment["ANSIBLE_ROLES_PATH"] = os.pathsep.join(
            str(path) for path in self._contributions.roles_path
        )
        environment["ANSIBLE_LOCAL_TEMP"] = str(
            self._settings.state_dir / "ansible-local"
        )
        Path(environment["ANSIBLE_LOCAL_TEMP"]).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        # Collections, for the same reason. Not created here: the installer and
        # image fetch into it, and an empty directory must fail loudly.
        environment.setdefault(
            "ANSIBLE_COLLECTIONS_PATH", str(self._settings.state_dir / "collections")
        )
        # Where the inventory plugin reads the fleet; absolute, because Ansible
        # runs from `ansible_dir`.
        environment["BLITZE_DATABASE_PATH"] = str(
            self._settings.database_path.resolve()
        )
        # Declared capability secrets, through the environment rather than
        # `--extra-vars`, which every user on the controller can read in the
        # process table.
        for name, value in self._contributions.environment.items():
            environment[name] = value.get_secret_value()
        return environment

    def _prune_logs(self) -> None:
        """Keep the newest ``run_log_retention`` logs, pruned on every run."""
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
        """Point at the log for a run that failed before any host was reported."""
        if hosts or status is RunStatus.SUCCEEDED:
            return None
        return f"Ansible reported no per-host result. The full output is at {log_path}."
