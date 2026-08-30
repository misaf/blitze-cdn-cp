"""Running Ansible, and turning what it reports into domain models.

Every invocation produces one retained artefact. An Ansible Runner event handler turns
structured task and recap events into an
:class:`~blitzecdn.domain.runs.AnsibleRun` — that is the only thing the
application layer sees. Raw terminal output goes to a log file under
``state_dir/logs`` that nothing here parses; it is kept so an operator has the
full account of a run, and so a process that died before Ansible could report
anything still leaves evidence behind.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import ansible_runner  # type: ignore[import-untyped]
from ansible_runner import (
    exceptions as runner_exceptions,
)

from blitzecdn.application.ports.edges import EdgeStore
from blitzecdn.config import Settings
from blitzecdn.domain.cache import PurgeEntry
from blitzecdn.domain.edges import EDGE_GROUP
from blitzecdn.domain.runs import (
    AnsibleRun,
    HostRun,
    RunStatus,
    TaskOutcome,
    TaskResult,
)
from blitzecdn.domain.validation import validate_edge_limit
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure.ansible_mapping import purge_entry_to_ansible
from blitzecdn.infrastructure.filesystem import atomic_write_yaml

__all__ = ["AnsibleRunner", "DeploymentLock"]

#: Exit code recorded for a run killed at its timeout, matching the shell
#: convention for a process ended by a signal after a deadline.
_TIMEOUT_RETURN_CODE = 124
_MAX_FAILURE_MESSAGE = 2000


class _RunnerEvents:
    """Translate Runner's event stream into the control-plane result contract."""

    def __init__(self) -> None:
        self._hosts: dict[str, dict[str, Any]] = {}

    def __call__(self, event: dict[str, Any]) -> bool:
        name = str(event.get("event") or "")
        data = event.get("event_data") or {}
        if not isinstance(data, dict):
            return True
        if name == "playbook_on_stats":
            self._apply_stats(data)
            return True
        host_name = data.get("host")
        if not isinstance(host_name, str) or not host_name:
            return True
        host = self._host(host_name)
        result = data.get("res") or {}
        if not isinstance(result, dict):
            result = {}
        if name == "runner_on_ok":
            if result.get("changed"):
                host["changes"].append(self._task(data, TaskOutcome.CHANGED))
            report = (result.get("ansible_facts") or {}).get("blitzecdn_report")
            if isinstance(report, dict):
                host["report"] = report
        elif name == "runner_on_failed" and not result.get("_ansible_ignore_errors"):
            host["failures"].append(self._task(data, TaskOutcome.FAILED, result))
        elif name == "runner_on_unreachable":
            host["failures"].append(self._task(data, TaskOutcome.UNREACHABLE, result))
        return True

    def hosts(self) -> tuple[HostRun, ...]:
        return tuple(
            HostRun.model_validate(self._hosts[name]) for name in sorted(self._hosts)
        )

    def _host(self, name: str) -> dict[str, Any]:
        return self._hosts.setdefault(
            name,
            {
                "host": name,
                "ok": 0,
                "changed": 0,
                "failed": 0,
                "unreachable": 0,
                "skipped": 0,
                "rescued": 0,
                "ignored": 0,
                "changes": [],
                "failures": [],
                "report": None,
            },
        )

    def _apply_stats(self, data: dict[str, Any]) -> None:
        for event_key, model_key in (
            ("ok", "ok"),
            ("changed", "changed"),
            ("failures", "failed"),
            ("dark", "unreachable"),
            ("skipped", "skipped"),
            ("rescued", "rescued"),
            ("ignored", "ignored"),
        ):
            counts = data.get(event_key) or {}
            if not isinstance(counts, dict):
                continue
            for name, count in counts.items():
                self._host(str(name))[model_key] = int(count)

    @staticmethod
    def _task(
        data: dict[str, Any],
        outcome: TaskOutcome,
        result: dict[str, Any] | None = None,
    ) -> TaskResult:
        message = None
        if result is not None:
            for key in ("msg", "stderr", "stdout", "reason", "exception"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    message = value.strip()[:_MAX_FAILURE_MESSAGE]
                    break
            message = message or "no message"
        return TaskResult(
            task=str(data.get("task") or "unnamed task").strip(),
            action=str(data.get("task_action") or ""),
            outcome=outcome,
            message=message,
            role=str(data["role"]) if data.get("role") else None,
        )


class DeploymentLock(AbstractContextManager["DeploymentLock"]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: object | None = None

    def __enter__(self) -> DeploymentLock:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise DeploymentBusyError("another deployment is already running") from exc
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            stream = self._stream
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]
            self._stream = None


class AnsibleRunner:
    """Runs Ansible against the fleet the control plane records.

    ``edges`` is the same store the ``blitzecdn`` inventory plugin reads. It is
    injected rather than opened here because this needs it for one thing only —
    expanding a ``--limit`` into explicit host names — and because that
    expansion must be answered from the identical rows Ansible is about to be
    given. Reading a separate copy is precisely the drift that removing the
    static inventory file was meant to end.
    """

    def __init__(self, settings: Settings, edges: EdgeStore) -> None:
        self._settings = settings
        self._edges = edges

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
        return self._execute(
            playbook=self._settings.playbook_path,
            variables=variables,
            host_limit=None,
            timeout=120,
            syntax_check=True,
        )

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun:
        self._validate_paths()
        return self._execute(
            playbook=self._settings.playbook_path,
            variables=self._settings.generated_vars_path,
            host_limit=host_limit,
            timeout=self._settings.deployment_timeout_seconds,
            check=check,
            targeted=self._targeted(host_limit),
        )

    def run_acme_challenge(
        self, *, action: str, domain: str, token: str, validation: str = ""
    ) -> AnsibleRun:
        self._validate_paths()
        playbook = self._settings.acme_challenge_playbook_path
        if not playbook.is_file():
            raise ConfigurationError(
                f"ACME challenge playbook does not exist: {playbook}"
            )
        with self._run_vars(
            "acme-challenge",
            {
                "blitzecdn_acme_action": action,
                "blitzecdn_acme_domain": domain,
                "blitzecdn_acme_token": token,
                "blitzecdn_acme_validation": validation,
            },
        ) as variables:
            return self._execute(
                playbook=playbook,
                variables=variables,
                host_limit=None,
                timeout=120,
            )

    def run_cache_purge(
        self,
        *,
        entries: Sequence[PurgeEntry],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> AnsibleRun:
        """Remove cached responses across the edges in scope.

        Not taken under the deployment lock. A purge writes no desired state
        and changes no configuration, and the case that matters most — a bad
        object being served while a deploy is midway through the fleet — is
        exactly when the lock would make it wait.
        """
        with self._run_vars(
            "cache-purge",
            {
                "blitzecdn_cache_purge_entries": [
                    purge_entry_to_ansible(entry) for entry in entries
                ],
                "blitzecdn_cache_purge_all": purge_all,
            },
        ) as variables:
            return self._playbook_run(
                self._settings.cache_purge_playbook_path, variables, host_limit
            )

    def run_origin_check(
        self, *, sites: list[dict[str, object]], host_limit: str | None = None
    ) -> AnsibleRun:
        """Ask each edge to connect to the origins it proxies to.

        The sites are handed over as run variables rather than read from the
        desired-state file: this takes no deployment lock, so the shared file
        may belong to a deploy in flight, and the check is a question about
        what is configured *now* rather than about what is being converged.
        """
        with self._run_vars(
            "origin-check", {"blitzecdn_origins_sites": sites}
        ) as vars_:
            return self._playbook_run(
                self._settings.origin_check_playbook_path, vars_, host_limit
            )

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun:
        """Collect cache and connection counters from the edges in scope.

        The counters come back through Runner events: ``blitzecdn_stats``
        publishes them as the ``blitzecdn_report`` fact and they arrive on
        ``HostRun.report``. Nothing is written to or read from the controller's
        filesystem, so a stats run leaves nothing behind to go stale.
        """
        with self._run_vars("stats", {}) as variables:
            return self._playbook_run(
                self._settings.stats_playbook_path, variables, host_limit
            )

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        """Strip BlitzeCDN configuration and TLS material from one edge.

        ``host_limit`` is required and never defaulted to the whole group: the
        other run methods treat an absent limit as "every edge", which for a
        teardown would empty the fleet. The caller names the host it is
        removing, and the playbook is fail-closed so a partial teardown keeps
        the inventory entry rather than stranding keys on a forgotten host.
        """
        with self._run_vars("decommission", {}) as variables:
            return self._playbook_run(
                self._settings.decommission_playbook_path, variables, host_limit
            )

    # -- Command construction ------------------------------------------

    def _playbook_run(
        self, playbook: Path, variables: Path, host_limit: str | None
    ) -> AnsibleRun:
        self._validate_paths()
        if not playbook.is_file():
            raise ConfigurationError(f"playbook does not exist: {playbook}")
        return self._execute(
            playbook=playbook,
            variables=variables,
            host_limit=host_limit,
            timeout=self._settings.deployment_timeout_seconds,
            targeted=self._targeted(host_limit),
        )

    @contextmanager
    def _run_vars(self, prefix: str, values: dict[str, object]) -> Iterator[Path]:
        """Write this run's variables to a path no other run can reach.

        A fixed filename per playbook made the file shared mutable state between
        overlapping runs, and overlap is ordinary here rather than exceptional:
        purge, stats and decommission all deliberately skip the deployment lock,
        the API serves them from a thread pool, and Dramatiq jobs are
        separate processes over one state directory. Whoever wrote last won — so
        a purge of two URLs could find ``purge_all: true`` in the file by the
        time its own playbook read it, empty the cache on every edge, and still
        return, and audit, a two-URL purge.

        Removed when the run ends: these carry one caller's request, not state
        anything reads afterwards.
        """
        directory = self._settings.run_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{prefix}-{uuid4().hex}.yml"
        try:
            atomic_write_yaml(path, values)
            yield path
        finally:
            path.unlink(missing_ok=True)

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

    def _targeted(self, host_limit: str | None) -> tuple[str, ...]:
        """The edges a limit resolves to, as names, for the run record.

        Answered from the same expansion :meth:`_limit` performs and therefore
        from the same rows Ansible is about to be given, so "what did this run
        aim at" cannot disagree with what it actually targeted.
        """
        resolved = self._limit(host_limit)
        if resolved == EDGE_GROUP:
            return tuple(edge.name for edge in self._edges.list_edges())
        return tuple(resolved.split(","))

    def _limit(self, host_limit: str | None) -> str:
        """Resolve a host limit to explicit edge names, or the whole group.

        The limit is expanded here against the recorded fleet rather than
        handed to Ansible as a pattern. Ansible's own syntax cannot express "this group,
        restricted to any of these names": ``:`` and ``,`` are both union
        separators and ``&`` binds only to the term beside it, so
        ``blitzecdn_edges:&a,b`` means "(edges also matching a) or b" and would
        happily reach a host outside the group. Expanding to a literal list of
        names taken *from* the group removes the question — a limit cannot name
        a host the control plane does not already manage as an edge.

        The second benefit is diagnostic: a typo fails here, naming the edges
        that do exist, instead of becoming Ansible's "skipping: no hosts
        matched" and a deploy that reports success having converged nothing.
        """
        validated = validate_edge_limit(host_limit)
        if validated is None:
            return EDGE_GROUP
        known = [edge.name for edge in self._edges.list_edges()]
        matched = [
            name
            for name in known
            if any(fnmatch(name, pattern) for pattern in validated.split(","))
        ]
        if not matched:
            raise ConfigurationError(
                f"host limit {validated!r} matches none of the configured edges: "
                + (
                    ", ".join(known)
                    or "no edges are registered; add one with 'blitzecdn edge add'"
                )
            )
        return ",".join(matched)

    # -- Execution -----------------------------------------------------

    def _execute(
        self,
        *,
        playbook: Path,
        variables: Path,
        host_limit: str | None,
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
        events = _RunnerEvents()
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
                    host_limit=host_limit,
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
        host_limit: str | None,
        environment: dict[str, str],
        timeout: int,
        check: bool,
        syntax_check: bool,
        event_handler: _RunnerEvents,
    ) -> Any:
        """Execute through Ansible Runner without exposing it above this adapter."""
        options = ["--extra-vars", f"@{variables}"]
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
                limit=self._limit(host_limit),
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
        # Always set, even when empty, so `lookup('env', ...)` in group_vars
        # resolves deterministically instead of inheriting a stray value from
        # the operator's shell.
        environment["BLITZE_MAXMIND_ACCOUNT_ID"] = self._settings.maxmind_account_id
        environment["BLITZE_MAXMIND_LICENSE_KEY"] = (
            self._settings.maxmind_license_key.get_secret_value()
        )
        environment["BLITZE_UNDER_ATTACK_SECRET"] = (
            self._settings.under_attack_secret.get_secret_value()
        )
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
