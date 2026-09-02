"""This capability's Ansible implementation, and where it landed on disk.

The two roles and the two plays that carry out a purge and read the counters
ship *inside this wheel*, beside the Python that asks for them. That is what
makes the capability a whole vertical slice: installing the distribution brings
the deployment implementation with it, and uninstalling takes it away, with no
directory in the control plane's checkout to add to or prune.

Located through :mod:`importlib.resources` rather than by counting ``..`` from
``__file__``. The difference matters in exactly the case that has to work: a
wheel installed into a virtualenv on a controller, where there is no repository
and no working directory to be relative to.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["CACHE_PURGE_PLAYBOOK", "EDGE_ROLE", "ROLES_PATH", "STATS_PLAYBOOK"]


def _directory() -> Path:
    """This package's directory as a real filesystem path.

    Ansible opens roles and playbooks by path, so a ``Traversable`` that is not
    one — a package imported from inside a zip — cannot be used at all, and
    saying so here beats a role that is reported missing at deploy time. Wheels
    are unpacked on install, so this is the ordinary case and not a fallback.
    """
    anchor = resources.files(__name__)
    if not isinstance(anchor, Path):
        raise RuntimeError(
            "blitzecdn-cache must be installed as an unpacked distribution: "
            "Ansible resolves its roles and plays by filesystem path, and this "
            f"installation exposes them as {type(anchor).__name__}."
        )
    return anchor


#: Handed to the control plane through ``blitzecdn_ansible_contributions``.
#: Everything under it is a role Ansible may resolve by name.
ROLES_PATH = _directory() / "roles"
EDGE_ROLE = "blitzecdn_cache_config"

#: Passed straight to ``PlaybookRunner.run_playbook``. Core stages the
#: variables, expands the host limit and applies the timeout; which play runs
#: is this package's business and travels with it.
CACHE_PURGE_PLAYBOOK = _directory() / "playbooks" / "cache-purge.yml"
STATS_PLAYBOOK = _directory() / "playbooks" / "stats.yml"
