"""The controller's edge ``known_hosts``: scanning a host, and recording its keys.

The file is ``<state>/.ssh/known_hosts``, which the controller's SSH
configuration names for every host but the loopback pair. Those two the
control-plane role records in ``known_hosts.loopback`` on each converge; this
file it never rewrites, so what is enrolled here survives an update.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from blitzecdn.capabilities.edges.domain import HostKey, known_hosts_pattern
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ExecutionError

__all__ = ["KnownHosts"]


class KnownHosts:
    """Edge host keys, kept in the file the controller's SSH client reads."""

    def __init__(self, settings: Settings, *, timeout_seconds: int = 10) -> None:
        self._directory = settings.state_dir / ".ssh"
        self._timeout = timeout_seconds

    @property
    def path(self) -> Path:
        return self._directory / "known_hosts"

    @property
    def managed(self) -> bool:
        """Whether SSH runs with the controller's own configuration.

        The same test `PlaybookExecutor` makes before adding ``-F``: without
        that file nothing points SSH at this ``known_hosts``.
        """
        return (self._directory / "config").is_file()

    def scan(self, host: str, port: int) -> tuple[HostKey, ...]:
        """Every key the host offers. Unverified: what it says, not what it is."""
        argv = ["ssh-keyscan", "-T", str(self._timeout), "-p", str(port), "--", host]
        try:
            completed = subprocess.run(  # noqa: S603 - argv, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ExecutionError(
                f"could not scan {host} for host keys: {error}"
            ) from error
        keys = _parse(completed.stdout, known_hosts_pattern(host, port))
        if not keys:
            detail = completed.stderr.strip().splitlines()
            raise ExecutionError(
                f"{host} port {port} offered no SSH host key"
                + (f": {detail[-1]}" if detail else "")
            )
        return keys

    def trusted(self, host: str, port: int) -> tuple[HostKey, ...]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        return _parse(text, known_hosts_pattern(host, port))

    def trust(self, host: str, port: int, keys: tuple[HostKey, ...]) -> None:
        """Replace whatever this file trusted for the host with ``keys``.

        Replaced, not appended: a key that rotated away would otherwise stay
        acceptable for good. Other hosts' lines, comments, markers and hashed
        entries are kept as they are.
        """
        pattern = known_hosts_pattern(host, port)
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        kept = [line for line in lines if not _names(line, pattern)]
        kept += [f"{pattern} {key.key_type} {key.key}" for key in keys]
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._directory, prefix=".known_hosts."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("".join(f"{line}\n" for line in kept))
            Path(temporary).chmod(0o600)
            Path(temporary).replace(self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def _names(line: str, pattern: str) -> bool:
    """Whether a plain (unmarked, unhashed) entry lists ``pattern`` among its hosts."""
    fields = line.split()
    if len(fields) < 3 or fields[0].startswith(("#", "@", "|")):
        return False
    return pattern in fields[0].split(",")


def _parse(text: str, pattern: str) -> tuple[HostKey, ...]:
    keys: list[HostKey] = []
    for line in text.splitlines():
        if not _names(line, pattern):
            continue
        _, key_type, key, *_ = line.split()
        try:
            keys.append(HostKey(key_type=key_type, key=key))
        except ValueError:
            continue
    return tuple(keys)
