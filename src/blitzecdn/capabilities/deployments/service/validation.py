"""Answering whether desired state is coherent, without publishing anything.

Validation is a question, not a convergence, and the difference is the whole
reason it lives apart from the service: it takes no deployment lock, writes to
no path any other process reads, moves no deployment through the transition
table, and leaves nothing behind. The service's own rule — one deployment at a
time, under a cross-process lock, finalised in one transaction — does not apply
to any of it, and mixing the two put a method that must never touch
``generated_vars_path`` in the same class as the methods whose job is to write
it.

Held together as a class rather than a free function only because the question
has several collaborators and a caller should not have to name them at every
call site. It owns no state between calls.

Most of the answer is no longer computed here. Deriving the hosts, merging the
rules, asking each installed capability what it objects to and checking every
requested capability against the edges are all the compiler's, and they arrive
as findings on the release. What is left is the part a compiler cannot answer:
whether this *machine* is configured to run Ansible at all, and whether Ansible
itself will parse the play once the artifact is on disk.
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
from blitzecdn.capabilities.releases.domain import DESIRED_STATE_ARTIFACT, Release

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

        Compiles rather than prepares, so asking costs no history: an operator
        fixing a fleet runs this repeatedly, and each run would otherwise
        record a release nobody asked to deploy.

        Writes the compiled artifact to a scratch file rather than to
        ``generated_vars_path``. Validation is a question, not a publication,
        and it takes no lock — so writing to the real file would let it land
        between the moment a deploy in another process published its artifact
        there and the moment Ansible read it. A rollback is where that hurts
        most: the fleet would converge to current state while the rollback
        still rewrote canonical records to the old release's zones, leaving the
        control plane and the edges disagreeing in exactly the way rollback
        exists to end.
        """
        errors = self._runtime_errors()
        errors.extend(self._dns.validation_errors())
        release = self._releases.compile(host_limit=host_limit)
        errors.extend(finding.message for finding in release.findings)
        if not errors:
            with self._scratch_desired_state(release) as variables:
                run = self._runner.validate(variables)
            if not run.succeeded:
                # The one place a log is read back. `--syntax-check` executes no
                # play, so there is no structured result to explain a refusal —
                # the return code decides, and Ansible's own message is quoted
                # so the operator does not have to go and find it.
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
            self._write_yaml(path, release.artifact(DESIRED_STATE_ARTIFACT).document)
            yield path
        finally:
            path.unlink(missing_ok=True)
