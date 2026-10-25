# Atomic configuration activation

## Context

The previous STAGE phase prepared Docker and directories. Rendering still edited
live Nginx snippets, modules and enabled links independently. A syntax error
could be restored selectively, but an interruption could leave a partial tree on
disk for the next restart. A healthy yet stale public listener failed deployment
verification without returning the edge to its previous configuration.

## Decision

Each edge retains `/etc/blitzecdn/releases/<full-release-digest>/`. Staging writes
the configuration the controller rendered for the release — its Nginx
entrypoint, HTTP fragments, sites, module list and executable njs resources —
checks it against the controller's manifest (see
[decision 0020](0020-controller-rendered-configuration.md)), then seals a
manifest of file hashes and link targets. Links must stay
inside the candidate. Files and directories are flushed before publishing the
seal. A sealed tree is verified and reused without rerendering on retry. The
exact resolved runtime image is captured inside the seal; locally loaded images
use an immutable image ID when no repository digest exists.

A fresh isolated container validates the candidate with the selected image and
the same volumes as the serving container. Activation verifies the seal again.
The serving container mounts the **stable parent**, not a candidate directory or
a resolved symlink. It starts Nginx with `-c .../current/nginx.conf`; that file
contains absolute includes into its own candidate. Replacing `current` with
`os.replace` is one filesystem operation visible inside an existing container.
Old workers retain access to their previous njs resources during graceful reload.

Before switching, the edge writes and flushes a transaction journal containing
the previous pointer, Compose definition and deployed-image record. Retrying an
interrupted activation preserves that original snapshot. Compose starts or
updates the runtime, and Nginx explicitly reloads even when Compose sees an
unchanged service definition. Ordinary rendering handlers are suppressed while
writing a candidate.

The controller's external release-identity observation is the commit gate.
Failure requests rollback; rollback restores the pointer and runtime records,
restarts the exact previous Compose definition and checks its configuration and
local status. A failed first installation removes the failed container and
pointer because there is no prior service. A failed rollback remains explicit
in the deployment error, independently of the original Ansible result. A late
request cannot withdraw an already committed transaction or overwrite a newer
active release. Rollback selection does not require the rejected candidate to
remain readable; it validates the previous tree it is restoring.

## Boundaries

This is atomic **configuration selection**, not a transaction over the operating
system, firewall, provider services or in-flight network connections. Host
convergence still belongs to Ansible. Provider-maintained external data such as
GeoIP databases retains its own lifecycle, and so do capability credentials:
they live in the runtime contract's `paths.secrets`, outside every release
tree (see [decision 0020](0020-controller-rendered-configuration.md)). Managed TLS files retain their
content-addressed paths outside the configuration tree and are not pruned while
old releases can refer to them; unmanaged certificates remain external inputs.
Canonical TLS versions and controller material collection are implemented in
[decision 0015](0015-canonical-certificate-versions-and-collection.md). Edge
configuration and material retention remains conservative.

A controller crash leaves an uncommitted edge journal. The durable deployment
checkpoint resumes activation or verification and commits or withdraws it; there
is no edge-local timeout that independently rolls it back. The existing global
deployment lock is still required: database fences protect controller records,
and do not fence remote effects. Direct standalone role usage retains its
existing behavior; the production edge play requires a compiled digest and uses
the transaction path.

## Code and verification

- `ansible/roles/blitzecdn_config_release`: stage seal, activation journal,
  rollback and explicit commit; stdlib helper executes on the edge.
- `ansible/playbooks/edge.yml`: explicit phase selection and candidate writers.
- `blitzecdn_nginx` and `blitzecdn_edge_stack`: candidate entrypoint, stable
  parent mount, identical validation mounts, explicit activation reload.
- `capabilities/deployments/service/rollout.py`: external verification before
  commit and explicit rollback reporting on failure.
- `tests/contract/roles/test_configuration_transactions.py`: real filesystem
  activation, crash replay, tamper detection, first-install withdrawal, stale
  rollback refusal, and actual Ansible path/mount evaluation in check mode.
- Deployment and journey tests: failed activation/verification withdrawal,
  independent rollback errors, successful commit and checkpoint recovery.

The workspace tests do not replace privileged Ubuntu/Docker edge convergence.
That integration validation remains outstanding and is recorded in the rebuild
tracker.
