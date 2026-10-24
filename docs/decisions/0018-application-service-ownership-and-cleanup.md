# Application service ownership and failure cleanup

## Review findings

The final architecture review covered application assembly, service and port
boundaries, release preparation/compilation, provider registration, persistence
lifetime and published-contract enforcement. Existing capability ports and the
preparation/compiler split still describe useful boundaries; neither needs a
generic service container or another command dispatch layer.

Three concrete inconsistencies required changes:

- TLS services were stored in module-global WeakKeyDictionary caches. Cached
  certificate services retained the control plane through material references,
  so a weak key did not prevent the global cache retaining the whole graph.
- API startup ran outside its cleanup boundary, and a shutdown failure skipped
  subsequent cleanup. Composition failures also left owned persistence unclosed.
- Documentation and golden-file comparison still described optional sibling
  packages after consolidation into one application.

## Ownership

The application root constructs TLS configuration, certificate operations and
Automatic SSL after their collaborators are wired. Routes, CLI commands and
registration hooks consume those application-owned services. Capability builders
are ordinary factories; no module-global service or configuration caches remain.
Services are built once before requests or scheduled work can use them, avoiding
races during lazy initialization. Invalid TLS configuration fails composition.

This follows the same arrangement as deployments, DNS and edge operations.
Capability-local builders still choose local adapters; the application root
chooses shared collaborators. Test doubles replace the explicit service fields
without reaching into private global caches.

## Cleanup

API lifespan uses an ExitStack to drain its worker pool, stop runtime plugins and
close owned persistence, in that order. Each callback is registered immediately
after acquisition, and later cleanup still runs when startup or another callback
fails. Failure to construct the pool still closes the control plane.

If application composition fails, the root closes only the repository it created.
Caller-injected repositories remain the caller's responsibility. Worker lifecycle
already has nested cleanup protecting persistence and retains that behavior.

## Contract and documentation consistency

Frozen-contract comparisons now cover their entire committed application surface,
without filtering by importable package names. Delivery parity likewise checks
every declared operation. Dependency checks inspect every registration seam
(since [0019](0019-typed-features-and-extension-context.md), `feature.py`). Public interfaces and snapshot contents do not
change in this step.

The README describes the single distribution, current development commands,
service ownership and third-party extension boundary. Policy and configuration
documentation distinguishes shipped providers from target runtime requirements.
Historical decisions remain records of their original steps.

Regression tests cover API startup, pool construction, draining and shutdown
failures; owned versus injected persistence during failed composition; and garbage
collection of a closed worker with TLS services and scheduled closures. Local
checks do not establish Ubuntu deployment or disaster-recovery readiness. The user
explicitly deferred that environment verification on 2026-10-24.
