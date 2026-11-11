# Edge playbook

Code: `assets/ansible/playbooks/edge.yml`,
`capabilities/deployments/service/rollout.py`.

## How a rollout is paced

Each edge goes through four `ansible-playbook` runs, each a tag selection:

1. **Validate**, in check mode. This is the only phase a `plan` or the
   scheduled drift check runs.
2. **Stage** writes a candidate configuration tree and seals it.
3. **Activate** switches to it and reloads. The controller then verifies the
   public listener of each edge from outside.
4. **Commit** after verification succeeds, or **rollback** if activation or
   verification failed.

An untagged hand run stages and then activates.

The controller paces the fleet itself (`capabilities/deployments/service/rollout.py`)
rather than through the play's `serial`, so progress is a database row and an
interrupted rollout resumes where it stopped:

- **Canary first.** The first edge goes alone, through every phase, and must
  succeed before any other edge is touched. A resumed rollout whose canary
  already succeeded skips this.
- **Then batches.** The remaining edges go in batches of at most
  `deployment_batch_size` (default 10): one run per phase per batch, the run
  limited to that batch (`--limit edge-b,edge-c,...`). Each edge's outcome is read
  from Ansible Runner's per-host events. A batch is also cut short if its names
  would not fit in one host limit (512 characters).
- **Stop after a failure.** A failure in validate or stage stops the batch
  there: the failing edge is FAILED, the others in its batch and every later
  edge are SKIPPED and still serve what they had. A failure in activate or
  verify withdraws that edge's activation; the batch's other edges, already
  serving the release, finish verification and commit. Either way no later
  batch starts.
- **Check mode needs no canary.** `plan` and the drift check change nothing, so
  every batch, the first included, is full-size, and the record merges every
  batch's per-host result.

A real rollout over N edges therefore makes about `4 × (1 + ⌈(N − 1) / B⌉)`
playbook runs for batch size B, instead of `4 × N`, and a drift check about
`⌈N / B⌉`. Each run gathers facts on its hosts; a run takes about as long as the
slowest edge in it, bounded by `deployment_timeout_seconds`. Ansible's `forks`
(20 in `ansible.cfg`) caps how many edges in one run work at once, so batch
sizes above it add no parallelism. No per-phase timing has been measured yet;
fact gathering runs in every phase, and a first install also pulls the runtime
image during stage.

### The drift check and the deployment lock

The drift check publishes the release it checks against to the same
desired-state file a deployment uses, so it takes the deployment lock. It does
not keep it from a deployment that wants it: a refused attempt to take the lock
(a `deploy`, a worker's queued job, certificate issuance) leaves a marker beside
it, and the check, between batches, stops when it sees one. The edges it had not
reached are recorded as unattempted, it does not count towards the
`drift_check` failure streak, and the waiter gets the lock on its next attempt:
the CLI's `--wait` and the worker retry on their own.

## Failure policy

`any_errors_fatal: true` stops the play on every host in the run after the task
that failed on one of them. For a controller run that is one batch: the
controller then reads which edge failed and which were only stopped with it.
`max_fail_percentage` is deliberately not also set: it expresses the same rule
in a second dialect, and `any_errors_fatal` is the stricter of the two.

The play has no `serial`. A hand-run `ansible-playbook` against the whole
inventory converges every edge at once, so canary one edge with `--limit` first.

## Order

Pre-tasks, before anything on the host changes:

1. Assert the supported OS.
2. `blitzecdn_edge` establishes the shared runtime contract (host paths,
   listener sets, status endpoint, runtime image, capability data directory)
   that `blitzecdn_nginx`, `blitzecdn_edge_stack` and `blitzecdn_firewall` all
   read. A contract no edge could serve fails before the machine is touched.
   It also asserts that 443 is an HTTPS listener for QUIC to attach to; HTTP/3
   is one value in the contract, so the firewall and nginx cannot disagree.
3. Select the versioned configuration tree.
4. Install the container engine and prepare the persistent directories,
   runtime image and Compose project, all before the firewall: opening public
   ports on a host that cannot serve them advertises an edge that is not there.
   The container is not started yet.
5. Verify the runtime image, by the digest `prepare.yml` just pinned rather
   than the requested tag, so the image probed is the image that will serve.
6. Install the release probe token for `/.blitzecdn/release`. It is
   reconciled on every stage and activation, sealed release or not, so
   redeploying an unchanged release repairs an edge holding an old token.

Roles:

1. `blitzecdn_base`, `blitzecdn_kernel`. Host DNS resolution belongs to the
   resolver capability in the edge slot below.
2. **Edge capability slot.** The controller composes
   `blitzecdn_capability_roles` from installed features and passes it as an
   extra-var, so the play names no capability. After the pre-tasks, because a
   capability may need the engine, image or directories; before the firewall
   and the staged configuration, because the configuration depends on what
   capabilities provision and the configuration test proves the whole tree
   loads.
3. `blitzecdn_firewall`.
4. `blitzecdn_nginx`: host state the configuration depends on (certificates,
   ACME webroot). The configuration itself is rendered on the controller.
5. `blitzecdn_config_release` stages the rendered tree, checks it against the
   release manifest, validates it under the image the container will run, then
   starts, leaves alone, or replaces the container and proves it serves. It
   runs strictly after the configuration exists: a container started against
   an incomplete tree answers 444 for every customer.
6. **Host capability slot**, for what a capability does to the host once the
   edge is serving (SSH policy and Fail2Ban, owned by the hardening
   capability). After the firewall, so a host that fails firewall validation is
   never left key-only but unreachable; after the runtime has proved it serves,
   so nothing that could close the management path runs while the edge may
   still need repair. Order within the slot is the contributing package's.
