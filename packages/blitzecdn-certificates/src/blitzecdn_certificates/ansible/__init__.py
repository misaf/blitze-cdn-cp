"""The one play this capability runs on the edges, shipped inside its wheel.

Publishing an HTTP-01 challenge is issuance, not convergence: it exists only
because this distribution is installed, so the play belongs here rather than in
the control plane's Ansible tree, where it used to sit behind a core setting
named after ACME.

There is no roles directory and therefore no ``blitzecdn_ansible_contributions``
implementation. The play writes into the webroot the *core* ``blitzecdn_edge``
role fixes, and reuses that role to resolve it — one value for where challenges
are served from and where they are written, which is the whole reason the
contract exists.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["ACME_CHALLENGE_PLAYBOOK"]


def _directory() -> Path:
    anchor = resources.files(__name__)
    if not isinstance(anchor, Path):
        raise RuntimeError(
            "blitzecdn-certificates must be installed as an unpacked "
            "distribution: Ansible resolves its plays by filesystem path, and "
            f"this installation exposes them as {type(anchor).__name__}."
        )
    return anchor


ACME_CHALLENGE_PLAYBOOK = _directory() / "playbooks" / "acme-challenge.yml"
