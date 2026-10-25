"""The origins capability's Ansible roles and plays, located inside the package."""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["ORIGIN_CHECK_PLAYBOOK", "ROLES_PATH"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles and plays by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
ROLES_PATH = _DIRECTORY / "roles"

#: Passed to ``PlaybookRunner.run_playbook``.
ORIGIN_CHECK_PLAYBOOK = _DIRECTORY / "playbooks" / "origin-check.yml"
