# Edge playbook

Code: `assets/ansible/playbooks/edge.yml`,
`capabilities/deployments/service/rollout.py`.

## How a rollout is paced

The controller converges edges strictly one at a time. Each edge takes four
`ansible-playbook` runs, each limited to that edge (`--limit <edge>`):

1. **Validate**, in check mode. This is the only phase a `plan` or the
   scheduled drift check runs.
2. **Stage** writes a candidate configuration tree and seals it.
3. **Activate** switches to it and reloads. The controller then verifies the
   public listener.
4. **Commit** after verification succeeds, or **rollback** if activation or
   verification failed.

Phases are tag selections (`stage`, `activate`, `commit`, `rollback`); an
untagged hand run stages and then activates.

A failed edge stops the rollout before the next edge is touched. Because every
run is limited to one host, the play's `serial` batches (`1`, `10%`, `25%`,
`100%`) never take effect for a controller-driven deployment; they only shape a
hand-run `ansible-playbook` against the whole inventory. Parallel or batched
controller rollouts are tracked in issue 264.

## Failure policy

`any_errors_fatal: true` stops the play on the first failed edge, so in a
hand run a broken runtime image stops after the first batch rather than
reaching the fleet. Each failing edge returns to its previous image on its
own. `max_fail_percentage` is deliberately not also set: it expresses the same
rule in a second dialect, and `any_errors_fatal` is the stricter of the two.

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
