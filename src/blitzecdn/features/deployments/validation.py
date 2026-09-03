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
has nine collaborators and a caller should not have to name them at every call
site. It owns no state between calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from blitzecdn.core.plugins import Severity
from blitzecdn.features.deployments.ports import (
    DeploymentRunner,
    DeploymentStore,
    DesiredStateRenderer,
    LogReader,
    SiteValidator,
    ZoneEditor,
)
from blitzecdn.features.deployments.snapshots import decode_snapshot

_LOGGER = logging.getLogger(__name__)

__all__ = ["DeploymentValidation"]


class DeploymentValidation:
    """Whether the current desired state could be converged at all."""

    def __init__(
        self,
        *,
        runtime_errors: Callable[[], list[str]],
        dns: ZoneEditor,
        deployments: DeploymentStore,
        validator: SiteValidator,
        runner: DeploymentRunner,
        renderer: DesiredStateRenderer,
        read_log: LogReader,
        run_dir: Path,
        output_limit_bytes: int,
    ) -> None:
        self._runtime_errors = runtime_errors
        self._dns = dns
        self._deployments = deployments
        self._validator = validator
        self._runner = runner
        self._renderer = renderer
        self._read_log = read_log
        self._run_dir = run_dir
        self._output_limit_bytes = output_limit_bytes

    def errors(self) -> list[str]:
        """Every reason the current desired state could not be converged.

        Renders to a scratch file rather than to ``generated_vars_path``.
        Validation is a question, not a publication, and it takes no lock — so
        writing to the real file would let it land between the moment a deploy
        in another process wrote its snapshot there and the moment Ansible read
        it. A rollback is where that hurts most: the fleet would converge to
        current state while the rollback still rewrote canonical records to the
        old snapshot's zones, leaving the control plane and the edges
        disagreeing in exactly the way rollback exists to end.
        """
        errors = self._runtime_errors()
        errors.extend(self._dns.validation_errors())
        snapshot = self._deployments.snapshot()
        errors.extend(self._plugin_errors(snapshot))
        if not errors:
            with self._scratch_desired_state(snapshot) as variables:
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

    def _plugin_errors(self, snapshot: str) -> list[str]:
        """Ask every installed plugin what it knows about each site.

        A blocking issue refuses the deployment; a warning is logged and
        converged anyway. Both are attributed to the plugin that raised them,
        because the operator reading the refusal may have installed it
        yesterday and has to know which package is objecting.
        """
        errors: list[str] = []
        for site in decode_snapshot(snapshot):
            for issue in self._validator.validate_site(site).issues:
                message = f"{issue.plugin}: {issue.site}: {issue.message}"
                if issue.severity is Severity.BLOCKING:
                    errors.append(message)
                else:
                    _LOGGER.warning("%s", message)
        return errors

    @contextmanager
    def _scratch_desired_state(self, snapshot: str) -> Iterator[Path]:
        """Render a snapshot somewhere only this call can see, then drop it."""
        self._run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._run_dir / f"validate-{uuid4().hex}.yml"
        try:
            self._renderer.render(snapshot, path)
            yield path
        finally:
            path.unlink(missing_ok=True)
