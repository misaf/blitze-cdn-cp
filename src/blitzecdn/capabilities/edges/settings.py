"""Edge lifecycle settings.

Nothing here is operator-configurable: the teardown play ships with the
platform's Ansible, and the edges capability is what runs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from blitzecdn.assets import ansible as core_ansible

__all__ = ["DECOMMISSION_PLAYBOOK"]

#: The teardown play ``edge decommission`` runs against one edge.
DECOMMISSION_PLAYBOOK: Final[Path] = core_ansible.DECOMMISSION_PLAYBOOK
