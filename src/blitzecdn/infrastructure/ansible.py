"""Running Ansible, and turning what it reports into domain models.

Every invocation produces two artefacts. The ``blitzecdn_result`` callback
writes a JSON document to a path belonging to that run, which this module reads
into an :class:`~blitzecdn.domain.runs.AnsibleRun` — that is the only thing the
application layer sees. The raw terminal output goes to a log file under
``state_dir/logs`` that nothing here parses; it is kept so an operator has the
full account of a run, and so a process that died before Ansible could report
anything still leaves evidence behind.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from types import TracebackType
from typing import IO
from uuid import uuid4

from blitzecdn.config import Settings
from blitzecdn.domain.edges import EDGE_GROUP
from blitzecdn.domain.runs import AnsibleRun, HostRun, RunStatus
from blitzecdn.domain.validation import validate_edge_limit
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure.process import terminate_process_group
from blitzecdn.ports import EdgeStore

__all__ = ["AnsibleRunner", "DeploymentLock"]

#: Exit code recorded for a run killed at its timeout, matching the shell
#: convention for a process ended by a signal after a deadline.
_TIMEOUT_RETURN_CODE = 124


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
        executes no play, so there is no host for the callback to report on.
        ``AnsibleRun.reported`` is false here by design, and the log file holds
        whatever Ansible said about the parse failure.

        The caller supplies the path rather than this reaching for
        ``generated_vars_path``: that file belongs to whichever deploy currently
        holds the lock, and validation must not write over it.
        """
        self._validate_paths()
        return self._execute(
            self._command(check=True, syntax_check=True, variables=variables),
            timeout=120,
            playbook=self._settings.playbook_path,
        )

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun:
        self._validate_paths()
        return self._execute(
            self._command(check=check, host_limit=host_limit),
            timeout=self._settings.deployment_timeout_seconds,
            playbook=self._settings.playbook_path,
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
            command = [
                self._settings.ansible_playbook,
                str(playbook),
                "--inventory",
                str(self._settings.inventory_path),
                "--extra-vars",
                f"@{variables}",
                "--limit",
                EDGE_GROUP,
            ]
            return self._execute(command, timeout=120, playbook=playbook)

    def run_cache_purge(
        self,
        *,
        entries: list[dict[str, str]],
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
                "blitzecdn_cache_purge_entries": entries,
                "blitzecdn_cache_purge_all": purge_all,
            },
        ) as variables:
            return self._playbook_run(
                self._settings.cache_purge_playbook_path, variables, host_limit
            )

    def run_stats(self, *, host_limit: str | None = None) -> AnsibleRun:
        """Collect cache and connection counters from the edges in scope.

        The counters come back through the callback: ``blitzecdn_stats``
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
        return self._execute(
            self._playbook_command(playbook, variables, host_limit),
            timeout=self._settings.deployment_timeout_seconds,
            playbook=playbook,
        )

    @contextmanager
    def _run_vars(self, prefix: str, values: dict[str, object]) -> Iterator[Path]:
        """Write this run's variables to a path no other run can reach.

        A fixed filename per playbook made the file shared mutable state between
        overlapping runs, and overlap is ordinary here rather than exceptional:
        purge, stats and decommission all deliberately skip the deployment lock,
        the API serves them from a thread pool, and the systemd timers are
        separate processes over one state directory. Whoever wrote last won — so
        a purge of two URLs could find ``purge_all: true`` in the file by the
        time its own playbook read it, empty the cache on every edge, and still
        return, and audit, a two-URL purge.

        Removed when the run ends: these carry one caller's request, not state
        anything reads afterwards.
        """
        from blitzecdn.infrastructure.filesystem import atomic_write_yaml

        directory = self._settings.run_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{prefix}-{uuid4().hex}.yml"
        try:
            atomic_write_yaml(path, values)
            yield path
        finally:
            path.unlink(missing_ok=True)

    def _playbook_command(
        self, playbook: Path, variables: Path, host_limit: str | None
    ) -> list[str]:
        self._validate_paths()
        if not playbook.is_file():
            raise ConfigurationError(f"playbook does not exist: {playbook}")
        return [
            self._settings.ansible_playbook,
            str(playbook),
            "--inventory",
            str(self._settings.inventory_path),
            "--extra-vars",
            f"@{variables}",
            "--limit",
            self._limit(host_limit),
        ]

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

    def _command(
        self,
        *,
        check: bool,
        syntax_check: bool = False,
        host_limit: str | None = None,
        variables: Path | None = None,
    ) -> list[str]:
        command = [
            self._settings.ansible_playbook,
            str(self._settings.playbook_path),
            "--inventory",
            str(self._settings.inventory_path),
            "--extra-vars",
            f"@{variables or self._settings.generated_vars_path}",
            "--limit",
            self._limit(host_limit),
        ]
        if syntax_check:
            command.append("--syntax-check")
        elif check:
            command.extend(("--check", "--diff"))
        return command

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
        self, command: list[str], *, timeout: int, playbook: Path
    ) -> AnsibleRun:
        run_id = uuid4().hex
        started_at = datetime.now(UTC)
        log_path = self._settings.log_dir / f"{run_id}.log"
        result_path = self._settings.run_dir / f"result-{run_id}.json"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        environment = self._environment(result_path)
        control_root = self._settings.state_dir / "ansible-control"
        control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            tempfile.TemporaryDirectory(dir=control_root) as control_path,
            # One combined stream. Ansible interleaves the two, and a log read
            # during an incident is easier to follow in the order things
            # actually happened than split across two files.
            log_path.open("wb") as log,
        ):
            environment["ANSIBLE_SSH_CONTROL_PATH_DIR"] = control_path
            # A process that could not be started at all still raises: the
            # runner either produces a real result or says it could not run,
            # and `RunStatus.UNSTARTED` is reserved for the record the
            # application writes in that case.
            return_code, timed_out = self._spawn(
                command, environment, log, timeout=timeout
            )

        status = (
            RunStatus.TIMED_OUT
            if timed_out
            else RunStatus.SUCCEEDED
            if return_code == 0
            else RunStatus.FAILED
        )
        hosts = self._read_result(result_path)
        self._prune_logs()
        return AnsibleRun(
            id=run_id,
            playbook=playbook.name,
            status=status,
            return_code=return_code,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            hosts=hosts,
            log_path=str(log_path),
            error=self._unreported_detail(status, hosts, log_path),
        )

    def _environment(self, result_path: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment["ANSIBLE_CONFIG"] = str(self._settings.ansible_dir / "ansible.cfg")
        environment["ANSIBLE_LOCAL_TEMP"] = str(
            self._settings.state_dir / "ansible-local"
        )
        Path(environment["ANSIBLE_LOCAL_TEMP"]).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        # Where blitzecdn_result writes this run's document. Per-run, so two
        # unlocked runs cannot overwrite each other's results.
        environment["BLITZE_RESULT_PATH"] = str(result_path)
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
        return environment

    def _spawn(
        self,
        command: list[str],
        environment: dict[str, str],
        log: IO[bytes],
        *,
        timeout: int,
    ) -> tuple[int, bool]:
        try:
            process = subprocess.Popen(  # noqa: S603 -- fixed executable and argument array
                command,
                cwd=self._settings.ansible_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise ExecutionError(f"unable to execute Ansible: {exc}") from exc
        try:
            return process.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            # Ansible forks a worker per host; killing only the playbook
            # leaves them converging edges behind our back.
            terminate_process_group(process, process.wait)
            return _TIMEOUT_RETURN_CODE, True
        except BaseException:
            # A CLI disconnect, Ctrl-C, or service stop must not orphan the
            # playbook and let it keep changing edges after its deployment
            # record has stopped receiving updates.
            terminate_process_group(process, process.wait)
            raise

    @staticmethod
    def _read_result(path: Path) -> tuple[HostRun, ...]:
        """Read the callback's document, then remove it.

        The models it feeds are what gets persisted, so the file itself is
        working state. A document that will not parse is treated as no document
        at all: the run then reports as unreported, and the log is the account.
        """
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        finally:
            path.unlink(missing_ok=True)
        if not isinstance(document, dict):
            return ()
        try:
            return tuple(
                HostRun.model_validate(host) for host in document.get("hosts") or []
            )
        except ValueError:
            return ()

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
