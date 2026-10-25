"""Ansible resources owned by origins.

Resolved through importlib resources so the installed application works without
a repository or a particular working directory. Role and playbook names remain
stable; their files travel beside their owning implementation in the one wheel.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["ORIGIN_CHECK_PLAYBOOK", "ROLES_PATH"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles and plays by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: Passed straight to ``PlaybookRunner.run_playbook``. Core stages the
#: variables, expands the host limit and applies the timeout; which play runs
#: is this capability's business and travels with it, rather than being a
#: core setting pointing at a file core does not own.
ORIGIN_CHECK_PLAYBOOK = _DIRECTORY / "playbooks" / "origin-check.yml"
