"""Running Ansible, and turning what it reports into domain models.

Every invocation produces one retained artefact. An Ansible Runner event handler turns
structured task and recap events into an
:class:`~blitzecdn.core.domain.runs.AnsibleRun` — that is the only thing the
application layer sees. Raw terminal output goes to a log file under
``state_dir/logs`` that nothing here parses; it is kept so an operator has the
full account of a run, and so a process that died before Ansible could report
anything still leaves evidence behind.

The parts, each with its own reason to change:

``events``
    Runner's event stream to :class:`~blitzecdn.core.domain.runs.AnsibleRun`
    components. The boundary that lets the layering tests refuse textual
    reasoning everywhere above.
``execution``
    One invocation: the environment, the artifact tree, the operator log and
    its retention, and the result-to-status mapping.
``hosts``
    A ``--limit`` expanded against the recorded fleet, which is what keeps a
    limit from naming a host the control plane does not manage.
``lock``
    The cross-process lock that makes "one deployment at a time" true across
    the API, the CLI and the worker.
``mapping``
    Domain values to the documents the playbooks read.
``runner``
    :class:`AnsibleRunner`: which playbook, which variables, which edges.
``variables``
    The per-run variable file, which is per-run because the runs that skip the
    deployment lock overlap routinely.
"""

from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.ansible.runner import AnsibleRunner

__all__ = ["AnsibleRunner", "DeploymentLock"]
