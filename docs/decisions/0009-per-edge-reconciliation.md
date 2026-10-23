# A rollout is per edge, and the progress is written down

Status: accepted; documents the design in
`capabilities.deployments.service.rollout`, the `deployment_targets` table, and
the serving probe in `capabilities.deployments.adapters.serving`.

## Context

A deployment was one Ansible run across the whole fleet. The play did the
sensible thing — `serial: [1, 10%, 25%, 100%]` with `any_errors_fatal`, so a
canary went first and the first failure stopped the rollout — and it did it in a
place where none of it could be resumed or observed.

**Progress lived in the event stream.** A controller that died halfway through
came back knowing a deployment had been abandoned and nothing about which edges
were already serving the new configuration. The next attempt started from the
beginning.

**Partial failure was an inference.** `AnsibleRun.unattempted` subtracted the
hosts that reported from the hosts that were targeted, so an edge missing from a
result might have succeeded quietly, failed unreported, or never been contacted.
"Half the fleet is on the new configuration and half on the old" — the fact an
operator most needs after a stopped rollout — was three counters and a guess.

**Concurrency was covered by a lock and nothing else.** The fleet-wide lock
stops two deployments overlapping, which is a different question from "is the
worker that is writing this result still the worker that owns this run". A
worker paused past its queue lease could come back and record an outcome for a
rollout somebody else had already resumed.

**Success meant "the tool said ok".** Ansible reported ok, `nginx -t` parsed the
tree, `nginx -s reload` returned zero. All three are true of an edge that
answers nothing: an unreachable upstream, an unclaimed listener, a certificate
the worker cannot read, a firewall closed in front of the lot. A deployment
could be green while the fleet served 502s or nothing at all.

## Decision

**The rollout moved out of the play and into the control plane, one edge at a
time.** The policy is unchanged — canary first, stop at the first failure — and
each edge's progress is now a row in `deployment_targets`.

**Five phases, and each is separable work.**

`PREPARE` publishes the release's artifact where Ansible will read it. Once per
deployment, before the loop: every edge converges the same bytes, and doing it
before the loop keeps a fleet with no edges registered behaving as it always
did.

`VALIDATE` is a check-mode run against this edge. It reports what would change
and changes nothing.

`STAGE` is `--tags stage`. The edge play tags every one of its pre-tasks that
way and no role at all, so this run installs the container engine, creates the
persistent directories, pulls and digest-pins the runtime image and proves that
image's Nginx can serve — and touches nothing that decides what is served. A
fleet can be staged ahead of a window and activated inside it.
`test_only_the_plays_preparation_carries_the_stage_tag` refuses a role task that
acquires the tag, because a staged edge that had started serving new
configuration would make the phase a lie.

`ACTIVATE` is the converge: render, prove the tree loads under the image that
will run it, reload.

`VERIFY` asks the edge, over the network, whether it is serving what the release
says it should. This is the phase the record exists for.

**Verification is a request, not a return code.** The controller connects to the
edge's public address, sends the customer's hostname in `Host` and — over TLS —
in SNI, and requires an HTTP response. The name is not resolved: DNS usually
does not answer with this edge at the moment a deployment finishes, so resolving
would test the DNS transition rather than the edge. The certificate's
chain is not verified, because an uploaded certificate from a private CA is a
supported mode and this controller may hold no chain for it. Its *names* are: the
probe reads the certificate the edge presented and refuses an edge whose subject
alternative names do not cover the hostname. With the chain unverified nothing in
the handshake looks at the certificate, so without that check an edge serving
another site's certificate would pass.

A response is the pass condition, and a 502 passes. That is deliberate: a 502 is
the edge working correctly and saying the origin is not, and an origin that was
already down was not this deployment's doing. Failing the rollout for it would
roll back a configuration that is fine and leave the origin exactly as broken.

**Fencing, on top of the lease.** Taking a rollout over increments the
deployment's `generation` and stamps every target row with it. Every subsequent
write names the generation it was read at, so the previous owner's next write
matches no row and it is *told* rather than left believing it succeeded. The
queue's lease decides who may converge an edge; this decides whose result about
that edge is recorded, and the two are different questions.

**Skipped is not failed.** The edges after a failure are recorded `SKIPPED`.
They are still serving whatever they had and nothing is known to be wrong with
them; reporting them as failures would send an operator to look at hosts that
are fine.

**An interrupted deployment is abandoned, and its rows say how far it got.** It
is not resumed: an operator starts a new deployment, which converges every edge
again — every phase is idempotent, so an edge that already had the release costs
time and changes nothing. What the rows buy is the answer to "which edges have
the new configuration" after a controller died, which used to be a guess.

Whoever finds the deployment ends it. The API's startup recovery abandons any
deployment left `RUNNING` when it starts with the lock free, and a worker whose
redelivered job finds its deployment `RUNNING` abandons it then — it holds the
deployment lock, the lock is the only way into a convergence, and the kernel
released it when the previous holder died, so nothing else can be converging
that deployment. Without the second, a worker that crashed and restarted under
a running API left its deployment `RUNNING` indefinitely. Either way the
deployment's job fails permanently with the reason rather than recording a
success for a rollout nobody finished.

`Rollout.converge` does skip edges already `SUCCEEDED` when called again for the
same deployment, and its tests hold that, but no command or route calls it a
second time. Resuming was considered and declined: a resumed rollback would have
to make adopting the rolled-back state safe to repeat, for a recovery an operator
can get by deploying again.

## Alternatives rejected

*Keep the fleet-wide run and record per-edge rows from its result.* It gives a
better report and nothing else: no record of an interrupted rollout, because the
run is over before the rows exist; no per-phase progress, because the run has no phases; and no
fence, because there is no per-edge write to fence.

*Verify from the edge instead of from the controller.* The edge already probes
its own status endpoint, and that endpoint is bound to loopback — it answers
whether nginx is up, which the reload already said. The question worth asking is
the visitor's, and only something outside the edge can ask it.

*Fail the rollout when an edge declares no public address.* That would make
declaring one mandatory, which is a product decision this phase does not get to
make on its own. `Edge.public_addresses` documents an empty list as "the same
address the controller connects to", so verification falls back to that and only
skips when there is nothing at all to knock on — logging when it does.

## Consequences

A full converge is now four runs per edge rather than one across the fleet:
validate, stage, activate, and a request. That is more Ansible than before and
it buys per-phase progress, a durable record of how far an interrupted rollout
got, and a deployment that cannot be green
while the fleet serves nothing. `blitzecdn deploy` from the CLI adds one more
check pass, because the operator's preview is a separate thing they asked for;
`--skip-preflight` gives it up.

`blitzecdn status <id>` prints how far each edge got, and
`GET /v1/deployments/{id}/targets` is the same answer for a client watching a
rollout.

A deployment with no edges registered succeeds and says so, rather than
succeeding silently as the fleet-wide run did — that run asked Ansible to
converge an empty group and got a zero return code.

`DriftReport.unattempted` is now the union of the last run's own view and the
target rows the check never reached, so a drift check that stopped early is not
in sync.

## Code and verification

- [The rollout](../../src/blitzecdn/capabilities/deployments/service/rollout.py)
- [A target, and its phases](../../src/blitzecdn/capabilities/deployments/domain/target.py)
- [Fenced writes](../../src/blitzecdn/capabilities/deployments/adapters/persistence.py)
- [The serving probe](../../src/blitzecdn/capabilities/deployments/adapters/serving.py)
- [The edge play's stage tag](../../src/blitzecdn/ansible/playbooks/edge.yml)
- [Abandoning a deployment its worker died in](../../src/blitzecdn/capabilities/deployments/service/convergence.py)
- [Rollout tests](../../tests/capabilities/deployments/test_rollout.py)
- [A worker's redelivery abandoning it](../../tests/capabilities/deployments/test_queue_workflow.py)
- [The probe against a real socket](../../tests/capabilities/deployments/test_serving_probe.py)
- [The stage tag's contract](../../tests/contract/roles/test_role_wiring.py)

## Admission fixes the target set

The deployment command creates pending per-edge rows in the same transaction as
the deployment and queued job. Execution reads that persisted plan rather than
matching the current roster again. New edges wait for another deployment; a
removed admitted edge fails the run before execution. Rollback selects the
current full fleet when admitted and fixes that set in the same way.
