"""Running Ansible, and turning what it reports into domain models.

Every run yields an :class:`~blitzecdn.core.domain.runs.AnsibleRun` built from
Runner's structured events; nothing above this package reads terminal output.
The raw output is kept as a log under ``state_dir/logs`` for operators, and as
evidence when a run dies before Ansible reports anything.

``events`` translates Runner events; ``execution`` runs one invocation;
``hosts`` expands a ``--limit`` against the recorded fleet; ``lock``
serialises deployments across processes; ``runner`` decides which playbook,
variables and edges; ``variables`` writes per-run variable files.
"""

from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.ansible.runner import AnsibleRunner

__all__ = ["AnsibleRunner", "DeploymentLock"]
