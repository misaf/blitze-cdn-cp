# 0002: Push configuration to edges with Ansible over SSH; no edge agent

Status: Accepted

## Context

An edge is a Linux host running the containerized Nginx runtime. Getting it
there takes package installs, a firewall, kernel tuning, SSH hardening,
certificates and the runtime image, as well as the Nginx configuration that
changes with every release. Both kinds of change must come from the
controller's desired state and report back per host.

The alternatives were an agent on every edge that pulls from the controller,
or a bespoke push protocol. Either would mean writing and securing a second
long-running program, plus its own update path.

## Decision

The controller runs `ansible-playbook` against registered edges over SSH,
through Ansible Runner (`core/ansible/runner.py`, `core/ansible/execution.py`):

- There is no inventory file. The inventory plugin reads registered edges and
  fleet settings from the controller database, the same rows
  `AnsibleRunner` expands a `--limit` against.
- The plays and roles ship inside the Python package. Each capability carries
  its own roles under `capabilities/<name>/ansible/` and contributes them
  through its feature (`AnsibleContribution`), so they resolve the same way in
  a checkout and in the control-plane image.
- Per-host results are read from Ansible Runner's event stream, so a batch's
  outcome is known per edge.
- One-off operations, such as a cache purge or a connections reading, are
  their own small plays run through the generic `run_playbook`. Core is given
  only their paths.

## Consequences

- Nothing runs on an edge between deployments except the runtime itself, and
  there is no agent to upgrade.
- The controller needs SSH access to every edge, from a management CIDR, with
  a host key it trusts. See "Edge SSH host keys" and "The controller's SSH
  key" in [operations](../operations.md).
- Edge-side scripts keep a lower Python floor (3.12) than the controller
  (see the [Ansible quick start](../../src/blitzecdn/assets/ansible/README.md)).
- Every phase is a full `ansible-playbook` run with fact gathering, which
  makes a rollout slower than a push of one file would be.
  [0004](0004-per-edge-rollout-phases.md) bounds the number of runs with
  batches.
- ansible-core is imported in process (templating, the inventory plugin),
  which has licence implications for the control-plane image; see
  [Licence](../../README.md#licence).
- An edge that is unreachable cannot be converged or rolled back by the
  controller until it is reachable again. It keeps serving what it had.
