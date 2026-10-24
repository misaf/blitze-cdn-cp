# Application capabilities and runtime providers

BlitzeCDN ships one application distribution. All business operations, pure
policy contracts, Ansible roles, Nginx fragments, and image build resources live
under `src/blitzecdn/` and travel in the same wheel. `pip install blitzecdn`
installs the complete application. There are no capability extras or sibling
package dependency bounds.

## Ownership

Business capabilities live under `capabilities/`. A capability uses `domain/`
for pure values, `service/` for application decisions, `adapters/` for I/O,
`ports.py` for consumer-owned protocols, and `api/` and `cli.py` or `cli/` for
delivery. Its policy contract may be `policy.py` or a `policy/` package.
`composition.py` builds its services from collaborators passed as named
arguments; it never receives the control plane. `feature.py` exports
`FEATURES`, typed `Feature` declarations of routes, commands, Ansible roles,
configuration and Nginx resources. Behaviour lives in `runtime.py`: a
`FeatureRendering` for what a site or the fleet renders and what blocks a
deployment, and a `FeatureOperations` for health checks, scheduled jobs and
startup or shutdown work. Each is a constant or a builder with named arguments
that the composition root calls; `runtime.py` imports no delivery code.

Cache operations belong to `cache`; issuing and automatic SSL belong to `tls`;
origin probing belongs to `origins`; archive recovery belongs to `backup`.
Serving owns shared policy composition and virtual-host values. DNS owns policy
authoring and host derivation. Neither owns another capability's lifecycle.

Focused implementations of external edge services live under `providers/`:
HTTP/3, GeoIP, host hardening and DNS resolver configuration. They contain
registration, Ansible roles and Nginx resources, rather than business services
or HTTP endpoints. Strategies such as gzip and Brotli remain values and
implementations of compression, not separate application capabilities.

## Assembly and execution

`composition.control_plane.BUILTIN_FEATURES` is the explicit application
roster of feature modules. Every shipped feature is required and reports the
application's version.
A missing or broken built-in fails startup. Package presence never enables or
disables an internal feature. Site policy, provider configuration and the
edge's declared capabilities decide how resources are used.

The registry collects declarations from features and external plugins alike,
and binds behaviour in two stages: rendering before the release compiler is
built, from configuration and adapters only, and operations after every
service exists. No feature or hook ever receives the composition root. Business calls
use services through their declared ports; there is no hook-based business
command bus. Configuration that needs no runtime registration stays a pure
value. Role search paths and renderer insertion slots are composed from the
registered contributions, rather than duplicated in scripts.

Provider-specific settings are scoped configuration declarations. Their
credential and portability rules stay with their owner. Backup composition
receives those declarations explicitly; its filesystem adapter does not reach
into the CLI or construct a control plane during recovery.

## Extensions and contracts

Third-party integrations may register through `blitzecdn.plugins` by
implementing the hooks in `core.plugins.hooks`. This is an external extension
boundary, not how the application assembles itself. A hook that is not static
receives an `ExtensionContext` of ports and configuration — never the control
plane or a service. The frozen hook ABI (version 2) and public SDK remain the
contract for those integrations. An external extension must not claim a
shipped feature's name.

Cross-capability imports use domain values, policy, ports and public reporting.
API response-model reuse belongs to API delivery. Concrete implementations from
different capabilities are connected by the application composition root.
Domain and policy modules import no I/O or delivery framework.

`just check-quick` exercises application behavior, contracts, role rendering and
layering. `just test-installation` proves the one wheel contains all required
resources and runs outside the checkout. The former attach/detach and core-only
installation modes have been retired with the independent distributions.
