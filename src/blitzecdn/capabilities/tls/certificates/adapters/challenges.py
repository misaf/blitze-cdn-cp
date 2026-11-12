"""Put an HTTP-01 challenge on the edges, or take it off again."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable

from blitzecdn.capabilities.tls import ansible
from blitzecdn.capabilities.tls.certificates.domain import ChallengeEdges
from blitzecdn.core.domain.runs import RunStatus
from blitzecdn.core.exceptions import ExecutionError
from blitzecdn.core.ports.operations import PlaybookRunner

__all__ = ["PlaybookChallenges"]

_LOGGER = logging.getLogger(__name__)

# The CA chooses these values and the playbook writes them into files on the
# edges, so each is held to the alphabet ACME defines for it before it leaves
# this process. The playbook asserts the same patterns on its side.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_VALIDATION = re.compile(r"^[A-Za-z0-9_.-]{1,2048}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")

# Read through the module so a test can drive the allowance without sleeping.
_monotonic = time.monotonic


class PlaybookChallenges:
    """Runs the ``acme-challenge`` play on the edges a hostname resolves to.

    Only those edges are targeted, so one edge that is down cannot block every
    certificate in the fleet; the ones left out are logged by name.

    ``timeout_seconds`` bounds the whole call: the DNS lookups that pick the
    edges are taken off it, and the play is stopped by Ansible Runner at what
    is left, or not started when nothing is.
    """

    def __init__(
        self,
        *,
        playbooks: PlaybookRunner,
        challenge_edges: Callable[[str], ChallengeEdges],
    ) -> None:
        self._playbooks = playbooks
        self._challenge_edges = challenge_edges

    def present(
        self, domain: str, token: str, validation: str, *, timeout_seconds: int
    ) -> None:
        if not _VALIDATION.fullmatch(validation):
            raise ExecutionError("the CA sent an invalid HTTP-01 validation value")
        self._run("present", domain, token, validation, timeout_seconds)

    def absent(self, domain: str, token: str, *, timeout_seconds: int) -> None:
        self._run("absent", domain, token, "", timeout_seconds)

    def _run(
        self,
        action: str,
        domain: str,
        token: str,
        validation: str,
        timeout_seconds: int,
    ) -> None:
        if not _DOMAIN.fullmatch(domain) or not _TOKEN.fullmatch(token):
            raise ExecutionError("the CA sent an invalid HTTP-01 challenge")
        started = _monotonic()
        edges = self._challenge_edges(domain)
        # The lookups have DNS timeouts of their own; what they took is not the
        # play's to spend.
        remaining = math.floor(timeout_seconds - (_monotonic() - started))
        if remaining < 1:
            raise ExecutionError(
                f"timed out after {timeout_seconds}s finding the edges for {domain} "
                f"before trying to {action} its HTTP-01 challenge"
            )
        if edges.skipped:
            _LOGGER.info(
                "challenge for %s (%s) placed on %s; not on %s, which %s does not "
                "resolve to",
                domain,
                action,
                ", ".join(edges.targets),
                ", ".join(edges.skipped),
                domain,
            )
        run = self._playbooks.run_playbook(
            name="acme-challenge",
            playbook=ansible.ACME_CHALLENGE_PLAYBOOK,
            host_limit=",".join(edges.targets) or None,
            variables={
                "blitzecdn_acme_action": action,
                "blitzecdn_acme_domain": domain,
                "blitzecdn_acme_token": token,
                "blitzecdn_acme_validation": validation,
            },
            timeout_seconds=remaining,
        )
        if run.status is RunStatus.TIMED_OUT:
            raise ExecutionError(
                f"timed out after {timeout_seconds}s trying to {action} the HTTP-01 "
                f"challenge for {domain} (full output: {run.log_path})"
            )
        if not run.succeeded:
            raise ExecutionError(
                f"could not {action} the HTTP-01 challenge for {domain}: "
                f"{run.summary()} (full output: {run.log_path})"
            )
