"""This capability's Ansible implementation, and where it landed on disk.

The role that probes an origin from an edge and the play that runs it across
the fleet ship *inside this wheel*, beside the Python that asks for them. That
is what makes the capability a whole vertical slice: installing the
distribution brings the deployment implementation with it, and uninstalling
takes it away, with no directory in the control plane's checkout to add to or
prune.

There is no ``EDGE_ROLE`` and no ``HOST_ROLES`` here. This capability converges
nothing on a deploy — its role is reached only by its own play, on demand — so
it contributes a search path and no slot at all, the same shape
``blitzecdn-cache``'s purge and statistics roles have.

Located through :func:`blitzecdn.core.runtime.resources.package_directory` rather than
by counting ``..`` from ``__file__``. The difference matters in exactly the
case that has to work: a wheel installed into a virtualenv on a controller,
where there is no repository and no working directory to be relative to.
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
#: is this package's business and travels with it. Core used to hold this path
#: as ``Settings.origin_check_playbook_path``, pointing at a file a detached
#: package would take with it.
ORIGIN_CHECK_PLAYBOOK = _DIRECTORY / "playbooks" / "origin-check.yml"
