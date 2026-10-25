"""Answering whether desired state is coherent, without publishing anything.

Validation takes no lock, writes nothing another process reads, and leaves
nothing behind, so it lives apart from the service that writes
``generated_vars_path``. Most findings come from the compiler on the release;
what is left is whether this machine can run Ansible and whether the play
parses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from blitzecdn.capabilities.deployments.ports import (
    DeploymentRunner,
    LogReader,
    Releases,
    YamlWriter,
    ZoneEditor,
)
from blitzecdn.capabilities.releases.domain import Release

__all__ = ["DeploymentValidation"]


class DeploymentValidation:
    """Whether the current desired state could be converged at all."""

    def __init__(
        self,
        *,
        runtime_errors: Callable[[], list[str]],
        dns: ZoneEditor,
        releases: Releases,
        runner: DeploymentRunner,
        write_yaml: YamlWriter,
        read_log: LogReader,
        run_dir: Path,
        output_limit_bytes: int,
    ) -> None:
        self._runtime_errors = runtime_errors
        self._dns = dns
        self._releases = releases
        self._runner = runner
        self._write_yaml = write_yaml
        self._read_log = read_log
        self._run_dir = run_dir
        self._output_limit_bytes = output_limit_bytes

    def errors(self, *, host_limit: str | None = None) -> list[str]:
        """Every reason the current desired state could not be converged.

        Compiles rather than prepares, so repeated validation records no releases.
        Writes the artifact to a scratch file: validating holds no lock, and writing
        the real file could swap the document between another process's deploy and
        Ansible reading it.
        """
        errors = self._runtime_errors()
        errors.extend(self._dns.validation_errors())
        release = self._releases.compile(host_limit=host_limit)
        errors.extend(finding.message for finding in release.findings)
        if not errors:
            with self._scratch_desired_state(release) as variables:
                run = self._runner.validate(variables)
            if not run.succeeded:
                # `--syntax-check` has no structured result; quote Ansible.
                errors.append(
                    self._read_log(run.log_path, limit=self._output_limit_bytes)
                    or run.summary()
                )
        return errors

    @contextmanager
    def _scratch_desired_state(self, release: Release) -> Iterator[Path]:
        """Write a release's artifact where only this call can see it."""
        self._run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._run_dir / f"validate-{uuid4().hex}.yml"
        try:
            self._write_yaml(path, release.execution_document())
            yield path
        finally:
            path.unlink(missing_ok=True)
