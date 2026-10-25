"""The cache capability's Ansible roles and plays, located inside the package."""

from __future__ import annotations

from blitzecdn.core.runtime.resources import package_directory

__all__ = ["CACHE_PURGE_PLAYBOOK", "EDGE_ROLE", "ROLES_PATH", "STATS_PLAYBOOK"]


_DIRECTORY = package_directory(
    __name__,
    resolves="Ansible resolves its roles and plays by filesystem path",
)


#: Declared in this feature's `AnsibleContribution`; every directory under it
#: is a role Ansible may resolve by name.
ROLES_PATH = _DIRECTORY / "roles"
EDGE_ROLE = "blitzecdn_cache_config"

#: Passed to ``PlaybookRunner.run_playbook``, which stages the variables and
#: applies the host limit and timeout.
CACHE_PURGE_PLAYBOOK = _DIRECTORY / "playbooks" / "cache-purge.yml"
STATS_PLAYBOOK = _DIRECTORY / "playbooks" / "stats.yml"
