# Canonical writes, and what a derivation does with state it refuses

Status: accepted; documents the design in `capabilities.dns.domain.hosts`,
`capabilities.dns.service`, and the compare-and-swap in
`capabilities.dns.adapters`.

## Context

Zones, rules, and records are canonical; virtual hosts are derived from them by
`derive_hosts` (see [0001](0001-zone-policy-and-composition.md)). That split
leaves two questions that no single module could answer on its own, and both
were answered by accident.

**The derivation was handed state it had not authored.** The editing services
validate what an operator writes, but three paths reach the stores without
passing them: a rollback adopts a snapshot wholesale, a backup restore writes
rows directly, and the certificates capability writes back through
`replace_domain` and `replace_rule` by design — `dns.domain.patch` says so, and
that is the arrangement, not a leak. A rule's overrides make it worse: they are
validated field by field, by handing the mapping to `DomainPatch`, which asks
nothing across two fields. So a rule that overrides `http3_enabled` on a zone
whose `ssl_mode` serves no TLS is storable through the ordinary rule editor and
is refused the first time the two are merged.

`derive_hosts` called `CdnSite.model_validate` with no handler around it. The
fleet is derived in one pass, so one such zone raised out of every read of
`platform.sites` — and `api.app` maps `ValidationError` to 422, so a corrupt
stored row reached an operator as though their own request were malformed. Two
other places in the tree already described the behaviour the code did not have:
`dns.domain.patch` and a zone-rule test both say the derivation "drops the host
it could not build instead of raising".

**Canonical writes read outside the boundary they wrote in.** Every editor here
is a read-modify-write: a patch is merged onto the whole stored document, so
the write carries every field, including the ones the operator never mentioned.
`update_domain` read the zone before opening its transaction. Two operators
patching different settings therefore left the second one's whole document on
top of the first one's change, and the setting that vanished was one nobody was
told about. Records had a compare-and-swap and zones and rules did not, which
left the aggregate with the widest reach — a zone's policy is inherited by
every hostname in it — as the one with no protection.

## Decision

**A derivation drops what it cannot build, and says so.** `derive_hosts`
returns the hosts it composed; `unservable_hosts` is the same derivation asked
what it dropped and why. Both are thin over one implementation, because a
second pass that re-decided what is servable would be free to disagree with the
first — and the disagreement would read as "validate says the fleet is fine and
the hostnames are still dark". `DnsService.validation_errors` reports the
refusals, so a deploy refuses to converge and names the hostnames, which is
where the rest of the tree already said an operator would meet them.

Raising was rejected for the reason above: it makes one broken zone the whole
fleet's problem, and it arrives disguised as a client error. Dropping silently
was rejected for the obvious one. The derived host's *name* is still claimed
even when its group is refused, because a name is a certificate directory — the
host after a broken one must not move into its place and then move back when
the break is fixed.

**Canonical editors read inside the Unit of Work, and write against what they
read.** `transaction` reserves the SQLite writer with `BEGIN IMMEDIATE` before
the read, so nothing can interleave between the merge and the write. That alone
closes the window; `expected=` on `replace_domain` and `replace_rule` is the
check behind it, refusing a document assembled from a version that no longer
exists — by a caller outside this package, or by a future edit that moves a
read back out. `require_transaction` refuses the compare-and-swap outside a
Unit of Work, where it would not be atomic. `HostService`'s automatic-SSL
guards moved inside the transaction for the same reason: the scan that proposes
an upgrade probes origins over the network first, so its opinion is always
stale, and an operator who leaves Auto during that window has to win.

The rule writeback also stopped using `model_copy`, which runs no validators.
The zone branch beside it used `model_validate` and was checked; the unchecked
branch was the one an issuer takes unattended.

## Consequences

A zone left inconsistent by a rollback now darkens its own hostnames and no
others, and `blitzecdn validate` names them before a deploy converges. It is
still storable — the rule's overrides are typed `Mapping[str, Any]`, so nothing
asks the cross-field questions at write time. Making that state unwritable is a
separate change to `Rule`, and this record is what makes it survivable in the
meantime.

Two concurrent zone patches now end with both settings applied rather than one
silently reverted. A caller holding a genuinely stale document gets
`ConflictError` instead of a lost update. The automatic-SSL reconciliation
holds the write lock across a derivation, which is affordable because it runs
on an interval and not on a request.

## Code and verification

- [Host derivation](../../src/blitzecdn/capabilities/dns/domain/hosts.py)
- [Zone editor](../../src/blitzecdn/capabilities/dns/service/zones.py)
- [Rule editor](../../src/blitzecdn/capabilities/dns/service/rules.py)
- [Host service](../../src/blitzecdn/capabilities/dns/service/hosts.py)
- [Zone and record store](../../src/blitzecdn/capabilities/dns/adapters/persistence.py)
- [Rule store](../../src/blitzecdn/capabilities/dns/adapters/rules.py)
- [Unit of Work](../../src/blitzecdn/core/persistence/engine.py)
- [DNS ports](../../src/blitzecdn/capabilities/dns/ports.py)
- [Host service tests](../../tests/capabilities/dns/test_host_service.py)
- [Zone rule tests](../../tests/capabilities/dns/test_zone_rules.py)
