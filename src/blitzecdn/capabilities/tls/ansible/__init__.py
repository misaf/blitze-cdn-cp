"""Ansible resources owned by tls.

Resolved through importlib resources so the installed application works without
a repository or a particular working directory. Role and playbook names remain
stable; their files travel beside their owning implementation in the one wheel.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["ACME_CHALLENGE_PLAYBOOK"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its plays by filesystem path",
)


ACME_CHALLENGE_PLAYBOOK = _DIRECTORY / "playbooks" / "acme-challenge.yml"
