"""Ansible resources owned by cache.

Resolved through importlib resources so the installed application works without
a repository or a particular working directory. Role and playbook names remain
stable; their files travel beside their owning implementation in the one wheel.
"""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["CACHE_PURGE_PLAYBOOK", "EDGE_ROLE", "ROLES_PATH", "STATS_PLAYBOOK"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles and plays by filesystem path",
)


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
#: Everything under it is a role Ansible may resolve by name.
ROLES_PATH = _DIRECTORY / "roles"
EDGE_ROLE = "blitzecdn_cache_config"

#: Passed straight to ``PlaybookRunner.run_playbook``. Core stages the
#: variables, expands the host limit and applies the timeout; which play runs
#: is this package's business and travels with it.
CACHE_PURGE_PLAYBOOK = _DIRECTORY / "playbooks" / "cache-purge.yml"
STATS_PLAYBOOK = _DIRECTORY / "playbooks" / "stats.yml"
