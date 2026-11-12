# 0004: Pace rollouts from the controller, per edge, in phases

Status: Accepted

## Context

An Ansible run that exits zero only means the tasks and the reload succeeded.
An upstream, listener, certificate or firewall rule can still be wrong, and a
bad configuration pushed to every edge at once takes the whole service down.
A rollout can also be interrupted by a worker restart, and must then resume
rather than start again or be abandoned.

Ansible's own `serial` would pace a play, but its progress lives only inside
that one `ansible-playbook` process.

## Decision

The controller paces the fleet itself
(`capabilities/deployments/service/rollout.py`) and records each edge's
progress as a row:

- Each edge goes through `PREPARE`, `VALIDATE` (check mode), `STAGE`,
  `ACTIVATE` and `VERIFY`. `VALIDATE` is the edge play in check mode;
  `STAGE` and `ACTIVATE` are tag selections of the same play.
- `VERIFY` is a request from outside the edge, to its public address, that
  must be answered with the release's digest. Failing it fails the
  deployment.
- The first edge goes alone as a canary and must succeed before any other is
  touched. The rest go in batches of `deployment_batch_size`.
- A failure stops the rollout once its batch is settled. An edge that failed
  after its serving state changed (`ACTIVATE` or `VERIFY`) is rolled back to
  its previous configuration. Later edges are `SKIPPED`, not `FAILED`: they
  are still serving what they had.
- Each takeover bumps a fence generation that every target write names, so a
  worker paused past its lease cannot record results over the worker that
  took over.

The [edge playbook](../design/edge-playbook.md) design note has the edge side:
tags, the failure policy and the run count per rollout.

## Consequences

- A bad release reaches one edge, not the fleet, and that edge is restored.
- An interrupted rollout resumes from its recorded phases. A resumed rollout
  whose canary already passed goes straight to batches.
- `plan` and the drift check run only `VALIDATE`. They change nothing and need
  no canary.
- A rollout makes about `4 × (1 + ⌈(N − 1) / B⌉)` playbook runs, so
  deployments are slower than a single fleet-wide run.
- Verification needs every edge to have a public address the controller can
  reach. An edge without one fails rather than passing unverified.
- A hand-run `ansible-playbook` against the whole inventory has no `serial`
  and converges every edge at once. Use `--limit` to canary by hand.
