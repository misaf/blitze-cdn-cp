"""Packaged traffic collection play and role."""

from blitzecdn.core.runtime.resources import package_directory

_DIRECTORY = package_directory(
    __name__, resolves="Ansible reads traffic collection assets by filesystem path"
)
ROLES_PATH = _DIRECTORY / "roles"
COLLECT_PLAYBOOK = _DIRECTORY / "playbooks" / "collect.yml"
