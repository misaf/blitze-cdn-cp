# Edge design notes

Why the edge's templates and playbooks are shaped the way they are. Each page
covers one subject and is the single place its rationale lives; the code
carries a one- or two-line comment that points here.

* [Origin connections](origin-connections.md): which origin a listener reaches,
  upstream pools, DNS re-resolution and the origin hop.
* [Path rules](path-rules.md): how path rules become locations and why their
  order holds against the firewall's paths.
* [Cache](cache.md): what is never cached, the cache key, cache levels, TTL
  overrides and revalidation.
* [Edge playbook](edge-playbook.md): phases, role order, the two capability
  slots and how a rollout is paced.
* [Edge container](edge-container.md): the runtime container's networking,
  privileges, shutdown and mounts.

## Comment rule

Inline comments keep only what a reader needs to change that exact line
safely. Rationale that spans more than a couple of lines, or that several files
share, belongs on one of these pages. When behaviour changes, update the page
once rather than chasing scattered comments.
