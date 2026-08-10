from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from fnmatch import fnmatch
from pathlib import Path
from types import TracebackType

from blitzecdn.config import Settings
from blitzecdn.domain.models import validate_edge_limit
from blitzecdn.domain.recap import parse_play_recap
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure.inventory import Inventory
from blitzecdn.infrastructure.process import terminate_process_group
from blitzecdn.ports import CommandResult

#: Re-exported so callers that think in terms of "the Ansible adapter" still
#: find both here. ``CommandResult`` is the DTO the runner port returns, and
#: ``parse_play_recap`` is a pure reading of that DTO's stdout.
__all__ = [
    "AnsibleRunner",
    "CommandResult",
    "DeploymentLock",
    "parse_play_recap",
]


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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def lock(self) -> DeploymentLock:
        return DeploymentLock(self._settings.deployment_lock_path)

    def validate(self) -> CommandResult:
        self._validate_paths()
        return self._execute(self._command(check=True, syntax_check=True), timeout=120)

    def run(self, *, check: bool, host_limit: str | None = None) -> CommandResult:
        self._validate_paths()
        return self._execute(
            self._command(check=check, host_limit=host_limit),
            timeout=self._settings.deployment_timeout_seconds,
        )

    def run_acme_challenge(
        self, *, action: str, domain: str, token: str, validation: str = ""
    ) -> CommandResult:
        self._validate_paths()
        if not self._settings.acme_challenge_playbook_path.is_file():
            raise ConfigurationError(
                "ACME challenge playbook does not exist: "
                f"{self._settings.acme_challenge_playbook_path}"
            )
        variables = self._settings.state_dir / "acme-challenge.yml"
        from blitzecdn.infrastructure.filesystem import atomic_write_yaml

        atomic_write_yaml(
            variables,
            {
                "blitzecdn_acme_action": action,
                "blitzecdn_acme_domain": domain,
                "blitzecdn_acme_token": token,
                "blitzecdn_acme_validation": validation,
            },
        )
        command = [
            self._settings.ansible_playbook,
            str(self._settings.acme_challenge_playbook_path),
            "--inventory",
            str(self._settings.inventory_path),
            "--extra-vars",
            f"@{variables}",
            "--limit",
            "blitzecdn_edges",
        ]
        return self._execute(command, timeout=120)

    def run_cache_purge(
        self,
        *,
        entries: list[dict[str, str]],
        purge_all: bool,
        host_limit: str | None = None,
    ) -> CommandResult:
        """Remove cached responses across the edges in scope.

        Not taken under the deployment lock. A purge writes no desired state
        and changes no configuration, and the case that matters most — a bad
        object being served while a deploy is midway through the fleet — is
        exactly when the lock would make it wait.
        """
        variables = self._write_run_vars(
            "cache-purge.yml",
            {
                "blitzecdn_cache_purge_entries": entries,
                "blitzecdn_cache_purge_all": purge_all,
            },
        )
        return self._execute(
            self._playbook_command(
                self._settings.cache_purge_playbook_path, variables, host_limit
            ),
            timeout=self._settings.deployment_timeout_seconds,
        )

    def run_stats(
        self, *, output_dir: Path, host_limit: str | None = None
    ) -> CommandResult:
        """Collect one JSON report per edge into ``output_dir``."""
        variables = self._write_run_vars(
            "stats.yml", {"blitzecdn_stats_output_dir": str(output_dir)}
        )
        return self._execute(
            self._playbook_command(
                self._settings.stats_playbook_path, variables, host_limit
            ),
            timeout=self._settings.deployment_timeout_seconds,
        )

    def run_decommission(self, *, host_limit: str) -> CommandResult:
        """Strip BlitzeCDN configuration and TLS material from one edge.

        ``host_limit`` is required and never defaulted to the whole group: the
        other run methods treat an absent limit as "every edge", which for a
        teardown would empty the fleet. The caller names the host it is
        removing, and the playbook is fail-closed so a partial teardown keeps
        the inventory entry rather than stranding keys on a forgotten host.
        """
        variables = self._write_run_vars("decommission.yml", {})
        return self._execute(
            self._playbook_command(
                self._settings.decommission_playbook_path, variables, host_limit
            ),
            timeout=self._settings.deployment_timeout_seconds,
        )

    def _write_run_vars(self, name: str, values: dict[str, object]) -> Path:
        from blitzecdn.infrastructure.filesystem import atomic_write_yaml

        path = self._settings.state_dir / name
        atomic_write_yaml(path, values)
        return path

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
        self, *, check: bool, syntax_check: bool = False, host_limit: str | None = None
    ) -> list[str]:
        command = [
            self._settings.ansible_playbook,
            str(self._settings.playbook_path),
            "--inventory",
            str(self._settings.inventory_path),
            "--extra-vars",
            f"@{self._settings.generated_vars_path}",
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

        The limit is expanded here against the inventory rather than handed to
        Ansible as a pattern. Ansible's own syntax cannot express "this group,
        restricted to any of these names": ``:`` and ``,`` are both union
        separators and ``&`` binds only to the term beside it, so
        ``blitzecdn_edges:&a,b`` means "(edges also matching a) or b" and would
        happily reach a host outside the group. Expanding to a literal list of
        names taken *from* the group removes the question — a limit cannot name
        a host the inventory does not already manage as an edge.

        The second benefit is diagnostic: a typo fails here, naming the edges
        that do exist, instead of becoming Ansible's "skipping: no hosts
        matched" and a deploy that reports success having converged nothing.
        """
        validated = validate_edge_limit(host_limit)
        if validated is None:
            return "blitzecdn_edges"
        known = [
            edge["name"]
            for edge in Inventory(self._settings.inventory_path).list_edges()
        ]
        matched = [
            name
            for name in known
            if any(fnmatch(name, pattern) for pattern in validated.split(","))
        ]
        if not matched:
            raise ConfigurationError(
                f"host limit {validated!r} matches none of the configured edges: "
                + (", ".join(known) or "the inventory has no edges")
            )
        return ",".join(matched)

    def _execute(self, command: list[str], *, timeout: int) -> CommandResult:
        environment = os.environ.copy()
        environment["ANSIBLE_CONFIG"] = str(self._settings.ansible_dir / "ansible.cfg")
        environment["ANSIBLE_LOCAL_TEMP"] = str(
            self._settings.state_dir / "ansible-local"
        )
        Path(environment["ANSIBLE_LOCAL_TEMP"]).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        # Always set, even when empty, so `lookup('env', ...)` in group_vars
        # resolves deterministically instead of inheriting a stray value from
        # the operator's shell.
        environment["BLITZE_MAXMIND_ACCOUNT_ID"] = self._settings.maxmind_account_id
        environment["BLITZE_MAXMIND_LICENSE_KEY"] = (
            self._settings.maxmind_license_key.get_secret_value()
        )
        control_root = self._settings.state_dir / "ansible-control"
        control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            tempfile.TemporaryDirectory(dir=control_root) as control_path,
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            environment["ANSIBLE_SSH_CONTROL_PATH_DIR"] = control_path
            try:
                process = subprocess.Popen(  # noqa: S603 -- fixed executable and argument array
                    command,
                    cwd=self._settings.ansible_dir,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ExecutionError(f"unable to execute Ansible: {exc}") from exc
            try:
                return_code = process.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                # Ansible forks a worker per host; killing only the playbook
                # leaves them converging edges behind our back.
                terminate_process_group(process, process.wait)
                timed_out = True
                return_code = 124
            except BaseException:
                # A CLI disconnect, Ctrl-C, or service stop must not orphan the
                # playbook and let it keep changing edges after its deployment
                # record has stopped receiving updates.
                terminate_process_group(process, process.wait)
                raise
            return CommandResult(
                return_code=return_code,
                stdout=self._read_bounded(stdout),
                stderr=self._read_bounded(stderr)
                + ("\nDeployment timed out" if timed_out else ""),
                timed_out=timed_out,
            )

    def _read_bounded(self, stream: object) -> str:
        stream.seek(0)  # type: ignore[attr-defined]
        data: bytes = stream.read(self._settings.output_limit_bytes + 1)  # type: ignore[attr-defined]
        if len(data) <= self._settings.output_limit_bytes:
            return data.decode("utf-8", errors="replace")
        return (
            data[: self._settings.output_limit_bytes].decode("utf-8", errors="ignore")
            + "\n[output truncated]"
        )
