from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from blitzecdn.config import Settings
from blitzecdn.exceptions import ConfigurationError, DeploymentBusyError, ExecutionError
from blitzecdn.infrastructure.process import terminate_process_group


@dataclass(frozen=True, slots=True)
class CommandResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


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

    def run(self, *, check: bool) -> CommandResult:
        self._validate_paths()
        return self._execute(
            self._command(check=check),
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

    def _command(self, *, check: bool, syntax_check: bool = False) -> list[str]:
        command = [
            self._settings.ansible_playbook,
            str(self._settings.playbook_path),
            "--inventory",
            str(self._settings.inventory_path),
            "--extra-vars",
            f"@{self._settings.generated_vars_path}",
            "--limit",
            "blitzecdn_edges",
        ]
        if syntax_check:
            command.append("--syntax-check")
        elif check:
            command.extend(("--check", "--diff"))
        return command

    def _execute(self, command: list[str], *, timeout: int) -> CommandResult:
        environment = os.environ.copy()
        environment["ANSIBLE_CONFIG"] = str(self._settings.ansible_dir / "ansible.cfg")
        environment["ANSIBLE_LOCAL_TEMP"] = str(
            self._settings.state_dir / "ansible-local"
        )
        Path(environment["ANSIBLE_LOCAL_TEMP"]).mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
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
