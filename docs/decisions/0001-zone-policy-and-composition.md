# Zone policy, derived hosts, and composition ownership

Status: accepted; documents the design merged in commits `3bec1d9` and `d5fe7cc`.

## Context

Serving policy previously lived on stored sites referenced by DNS records.
Maintaining that relationship required hostname projection updates, revision
tracking, repair operations, and checks for conflicting record-to-site links.
Moving policy onto zones and rules made the stored site a duplicate of canonical
state.

## Decision

Zones own default serving policy. Rules override it for matching hostnames.
Records identify the DNS answers and which hostnames are proxied. Virtual hosts
are derived from those three inputs rather than persisted separately.

The package-facing `CdnSite` and `SiteReader` contracts continue to represent the
hosts an edge serves. Certificate activation and automatic SSL upgrades write
back to the source zone or rule. A certificate for a rule-derived host must not
be written onto the zone and thereby affect unrelated hostnames.

Wire models remain explicit and validate through domain models. Parity tests
protect field coverage without making HTTP schemas the source of domain policy.

## Consequences

There is no stored hostname projection to repair or keep synchronized. Reads
perform derivation, and a single-host lookup currently scans the resulting hosts.
Deployment validation still checks that every proxied hostname has an effective
origin, including state loaded by restore or rollback.

## Deployment and rollback ownership

`DeploymentService` keeps synchronous deployment, queued execution, rollback,
drift checks, and recovery together because they share the deployment lock and
deployment lifecycle. Keeping their orchestration together makes lock ordering
and finalization visible in one service.

The adjacent modules own decisions that can be understood separately:

- `service.rollback` selects a snapshot, checks whether canonical state changed
  during convergence, and restores zones, records, and rules.
- `service.validation` checks desired state using a scratch file without taking
  the deployment lock or overwriting an active deployment's variables.
- `service.reporting` interprets recorded deployment results.

Rollback adoption runs inside the convergence service's transaction after the
canonical-state check. It restores zones and records before rules because
replacing zones cascades deletion to their rules. Hosts are derived on the next
read; there is no hostname resynchronization step.

## Composition boundary

`ControlPlane` remains the production composition root. It chooses concrete
adapters and invokes capability-local builders with explicit ports. The built-in
plugin roster belongs here because choosing installed capabilities is composition;
core discovery provides the loading mechanism without naming capabilities.

Entry layers receive services and ports rather than the concrete repository.
Optional packages use published contracts. The worker constructs a control plane
as an entry point, so composition accesses the queue through the runtime broker
instead of importing the worker.

Plugin discovery precedes adapter construction so the Ansible runner receives
the installed packages' role paths. Plugins receive the control plane only
after its services have been wired.

## Code and verification

- [DNS service](../../src/blitzecdn/capabilities/dns/service/zones.py)
- [Host derivation](../../src/blitzecdn/capabilities/dns/domain/hosts.py)
- [Composition root](../../src/blitzecdn/composition/control_plane.py)
- [Convergence service](../../src/blitzecdn/capabilities/deployments/service/convergence.py)
- [Rollback policy](../../src/blitzecdn/capabilities/deployments/service/rollback.py)
- [Validation](../../src/blitzecdn/capabilities/deployments/service/validation.py)
- [Reporting](../../src/blitzecdn/capabilities/deployments/service/reporting.py)
- [Deployment tests](../../tests/capabilities/deployments/test_deployments.py)
- [Queued workflow tests](../../tests/capabilities/deployments/test_queue_workflow.py)
- [Zone rule tests](../../tests/capabilities/dns/test_zone_rules.py)
- [Architecture tests](../../tests/architecture/test_layering.py)
