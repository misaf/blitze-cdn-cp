# One application with focused runtime providers

## Context

Ten independently installed distributions implemented features of one control
plane. Packaging determined which commands, routes, configuration declarations,
Ansible roles, Nginx fragments and runtime modules existed. Development, images
and host installation had different assembly rules. Capability contracts already
lived in the application, so separate wheels added dependency bounds and
attach/detach states without an independent product lifecycle.

## Decision

Ship one `blitzecdn` distribution, one version and one dependency lock. Remove
capability extras, workspace members, sibling dependency bounds and old import
namespaces. The installer, development recipes, CI and control-plane image
install the same application. Their configuration choices do not install or
remove Python implementations.

Business operations move into their owners:

| Capability | Implementation |
| --- | --- |
| cache | policy, purge, statistics, delivery and cache edge resources |
| tls | policy, certificate workflows, automatic SSL and ACME hooks |
| origins | origin probing and its reports |
| backup | archive creation and offline recovery |
| compression | policy and compression edge resources |
| security | filtering policy, request enforcement and challenge resources |

Focused infrastructure providers live under `blitzecdn.providers`: HTTP/3,
GeoIP, host hardening and resolver configuration. They own registration and
Ansible/Nginx resources rather than application services. Configuration and
site policy still decide their use; edge capability declarations and module
verification still constrain executable releases.

`BUILTIN_PLUGINS` is the explicit shipped roster. Every built-in is required
and reports the application's version. A missing built-in fails startup.
Third-party entry points remain an external extension seam; they no longer
assemble the application's own features. Existing registration hooks stay as
contribution adapters, not business calls or a command dispatcher.

Pure policy facades remain independent of service construction. Services consume
public contracts and consumer-owned ports. The application composition root
connects concrete cross-capability adapters. Origin probing has its own capability
so it does not give serving's shared-value package a dependency on edge execution.
Its public reporting functions can be consumed without importing its service.

Backup composition receives configuration declarations explicitly. Its file
adapter derives portable keys from injected values and never imports CLI
state or constructs a control plane. Offline recovery still opens no repository.
The origin-check playbook adapter used by automatic SSL is connected in the
application composition root rather than imported by TLS implementation wiring.

## Resources, contracts and installation

Roles, plays, Nginx fragments and Docker inputs stay beside their owning modules
and resolve through package resources from an installed wheel. Platform Ansible
stays capability-neutral. Role names, CLI verbs, HTTP routes, schema shapes and
Ansible arguments are unchanged. Frozen CLI, HTTP and Ansible rows change owner
to `blitzecdn`; comparison with the preceding snapshots proves their remaining
columns are identical. Hook ABI, public SDK and database snapshots are unchanged.

The obsolete core-only and sibling-package installation modes and their test
recipes are removed. The test suite no longer skips cases or disables cache and
compression based on whether old distributions are present. Behavioral tests
move into `tests/capabilities` and `tests/providers`. Tests of attach/detach
packaging are replaced with explicit assembly, single-manifest, resource
ownership, provider scope and installed-application contracts. Contract and
layering rules remain executable, with the new capability dependencies declared
and checked for cycles.

The isolated installation test builds the wheel, installs only the locked runtime
dependencies and that wheel into a fresh environment, runs outside the checkout,
and checks command discovery, every contributed resource directory and absence
of sibling distributions. It also checks that describing commands creates no
database. The dependency installation uses the lock directly rather than resolving
unbounded version ranges on every test run.

## Code and verification

- `pyproject.toml`, `uv.lock`, installer, image and CI: one installation unit.
- `composition/control_plane.py`: mandatory roster and concrete adapter binding.
- Capability `provider.py` modules and `providers/`: focused contribution owners.
- Backup composition/adapter boundary and public origin reporting.
- `tests/architecture/test_application_distribution.py`: assembly and ownership.
- `tests/architecture/test_application_installation.py`: actual installed wheel.
- Existing capability, provider, API, CLI, compiler, deployment and edge contracts
  exercise relocated behavior. Platform shell and Ansible checks remain required.

No live server is changed by this consolidation. Simplifying installation beyond
removing package-selection flags, separating schema initialization from repository
construction, and verifying recovery on a real Ubuntu edge remain subsequent work.
