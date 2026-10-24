# Working in this repository

BlitzeCDN is a control plane for containerised Nginx edges: Python decides,
Ansible converges. It is one application distribution, `blitzecdn`.

## Layout

- `src/blitzecdn/core/`: shared values, ports, persistence, configuration,
  registration machinery and the Ansible execution substrate.
- `src/blitzecdn/capabilities/`: business capabilities with `domain/`, `service/`,
  `adapters/`, `api/`, policy and consumer-owned ports.
- `src/blitzecdn/providers/`: focused implementations for HTTP/3, GeoIP, host
  hardening and resolver configuration, with their Ansible/Nginx resources.
- `src/blitzecdn/composition/`: application assembly and cross-capability wiring.
- `src/blitzecdn/ansible/` and `docker/`: platform execution and build resources.
- `tests/`: behavioral, architecture, delivery, edge and installed-wheel tests.
- `docs/decisions/`: rationale for decisions spanning multiple modules.

There are no sibling distributions, capability extras or core-only installation.
All runtime resources must ship in the one wheel. Read PLUGINS.md for ownership
and registration conventions, and COMPATIBILITY.md for published surfaces.

## Commands and validation

Use `just fmt`, `just types` and `just check-quick` while working. Run
`just test-one PATH` for focused checks, `just test-installation` for the actual
installed wheel, and `just check` before pushing. Tests use
`-n auto --dist=worksteal`; do not tune worker counts. The coverage floor is 85%.

Types cover source and shared test helpers. Test cases are unannotated;
`# type: ignore` in a case suppresses nothing. Import shared helpers explicitly.
Do not add wildcard imports or tests that merely mirror implementation details.

`tests/contract/frozen/` records published CLI, HTTP, extension ABI/SDK, Ansible
and schema interfaces. Regenerate with `just refreeze` only for intentional
surface changes, and inspect the diff. Never edit the snapshot to hide a failure.

## Architecture

Domain and policy values import no I/O or delivery frameworks. Services use
consumer-owned ports. Cross-capability imports reach public domain, policy,
ports and reporting modules; shared HTTP response representations belong to
API delivery. The application composition root connects concrete adapters.
Registration and composition are seams, not business logic.

`BUILTIN_PLUGINS` is the explicit shipped roster. Built-ins are mandatory;
external entry-point discovery is only for third-party integrations. A runtime
provider must not duplicate business services or grow a separate lifecycle.
Capability availability comes from contracts and the assembled provider set,
not from package installation checks.

Platform Ansible roles remain capability-neutral. Capability/provider roles,
playbooks and Nginx fragments live beside their owners. Resolve role paths and
slots through the same registry used by execution, never through a second list.
Resources must resolve from an installed wheel without a checkout.

## Domain invariants

- Sites are derived from zones, rules and records; they are never stored.
- Serving owns nested policy composition and effective-host values; DNS owns
  authoring and projection. The first matching rule supplies all overrides.
- Releases are immutable and content-addressed. The compiler is pure; a
  deployment names a retained release and never reconstructs its desired state.
- Background work is durable rows with leases and fences on the primary
  database. There is no external queue broker.
- Rollouts record per-edge progress and verify the externally served release.
  A successful reload alone is not evidence of correct activation.
- Managed TLS policy names exact certificate/key versions. Collection preserves
  canonical references, retained releases and metadata under the write boundary.
- BlitzeCDN records desired DNS but does not publish authoritative answers.

Nothing is deployed yet. Schema changes edit the single initial migration;
retired input versions are rejected without upcasters or compatibility facades.
Use `just release-version X.Y.Z` to update the application version, image pins
and lockfile together. Never change version strings independently.

Keep rationale in local docstrings where it is local and in an ADR where it
constrains several files. Update documentation when the arrangement changes.
