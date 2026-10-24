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
#: is this package's business and travels with it. Held by core as
#: ``Settings.origin_check_playbook_path``, it would be a setting pointing at a
#: file a detached package takes with it.
ORIGIN_CHECK_PLAYBOOK = _DIRECTORY / "playbooks" / "origin-check.yml"
