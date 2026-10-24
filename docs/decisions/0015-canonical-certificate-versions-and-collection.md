# Canonical certificate versions and reference-aware collection

## Context

Managed PEM files were content-addressed on the controller and in compiled
artifacts, but authored policy named mutable `fullchain.pem` and `privkey.pem`
paths. The certificate contributor resolved the latest metadata during every
compilation. Renewal could therefore change executable material without changing
the canonical inputs digest. Replaying restored inputs selected the newest pair
instead of the pair the operator restored. Superseded files were retained forever
because collection had no complete reference set.

## Decision

`CertificateVersion` is a frozen pair of SHA-256 digests owned by the TLS contract.
Managed policy paths name `fullchain-<digest>.pem` and `privkey-<digest>.pem`
under the derived host's directory. The host validates both the exact version
syntax and site ownership. The issuer writes the version returned by installation
into the zone or rule that authored that host. Upload and renewal now change
canonical desired state even when all other settings stay equal.

Compilation resolves the version in the captured site's policy. It verifies the
selected bytes against their digests and refuses missing or corrupt material;
it never substitutes the metadata pointer's latest pair. The contribution supplies
source paths and checksums, leaving the canonical destination paths intact.
Expected checksums come from the canonical version; a later file change must
fail execution verification rather than change the release's expected bytes.
Metadata continues to describe the latest stored certificate for inventory and
renewal; it does not choose a release's material. Read-only compilation can fail
if its old snapshot becomes unreachable during collection; release admission holds
the canonical write transaction through resource resolution, save and pruning,
so an admitted snapshot becomes retained before collection can run.

Input schema and compiler versions advance to 3. Input versions 1 and 2 are
explicitly retired without an upcaster. Existing unmanaged certificates and
provider-maintained data such as GeoIP retain their external lifecycle. Static
executable resources remain protected by the sealed configuration trees from
decision 0012.

## Collection roots and synchronization

`blitzecdn cert collect --json` collects superseded **controller** material. The
application command takes the deployment lock, then the canonical write unit of
work. Issuance uses the same deployment lock. Canonical writers, release admission
and pruning use the same database write boundary. Reading roots and deleting
unreachable files therefore occur without either authoring or issuance moving
the selected versions underneath the sweep.

The roots are:

- All zone policy and rule override certificate paths, including inactive rules
  and hosts that are not currently served.
- The canonical inputs of **every** retained release, without a pagination limit.
  Existing release retention protects releases named by deployments, including
  queued/running runs and rollback history.
- Every valid metadata pointer in the certificate store. This also protects a
  complete pair installed before an interrupted or failed canonical writeback.

Only regular files with exact owned content-addressed PEM names are candidates.
Unknown files and symlinks are skipped. Every metadata root and retained release
is validated before the first deletion; damaged roots abort collection. Metadata
site names must match their directories. Deletion is idempotent and restartable;
filesystem removal is not rolled back if deletion or audit recording later fails.
Every already removed file was unreachable before deletion. Successful sweeps
record an audit event listing reclaimed relative paths.

The command does not expire metadata inventory entries or choose release retention.
Deleting a site alone consequently does not discard its latest stored pair. It
does not remove edge files or configuration trees: old workers, active pointers
and activation journals on remote edges have independent references. Their
retention remains conservative until edge recovery has been exercised. There is
no scheduled collection and no controller deletion performed merely by importing
the package or compiling a release.

## Verification

Tests replay captured inputs after renewal and compare the full release digest;
exercise deployment-history retention and actual rollback to the old canonical
version; preserve references beyond the first release page and inactive rules;
cover missing metadata, interrupted installation, corrupted roots, interrupted
collection, symlink boundaries, missing/corrupt selected bytes, and the CLI.
Transaction tests assert that collection reads/deletes within one unit of work
and that resource resolution and release retention share their write boundary.

The CLI contract intentionally adds `cert collect`. Its absence from HTTP is
declared in delivery parity as controller-local filesystem maintenance. HTTP,
plugin ABI, SDK, Ansible and database schema snapshots are unchanged.
