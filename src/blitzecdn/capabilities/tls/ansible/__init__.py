"""The tls capability's Ansible roles and plays, located inside the package."""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["ACME_CHALLENGE_PLAYBOOK"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its plays by filesystem path",
)


ACME_CHALLENGE_PLAYBOOK = _DIRECTORY / "playbooks" / "acme-challenge.yml"
