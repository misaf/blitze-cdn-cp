# blitzecdn-cache

Cache purge and cache-effectiveness reporting, as an optional distribution.

Attaching it adds `POST /v1/cache/purge`, `POST /v1/cache/stats` (and the v2
pair), the `blitzecdn cache purge` command group, and the root `blitzecdn stats`
command.

```bash
uv add --package blitzecdn blitzecdn-cache     # attach, in this workspace
uv remove --package blitzecdn blitzecdn-cache  # detach
```

## What this package is not

It is not cache *policy*. A site's TTLs and its query-string mode are
`CachePolicy` in `blitzecdn.features.sites.policy`, part of the flat `CdnSite`
every edge renders, and they stay in the control plane whether or not this
package is installed. Edges keep caching exactly as configured with this
package detached — what disappears is the ability to *ask* them to drop
something, or to report on what they kept.

That is the `installed` / `enabled` / `configured` distinction: detaching this
package is not the same as setting a site's cache TTL to zero, and it changes
no desired state and no site's behavior.

Detaching is non-destructive: it owns no table and no migration, and the
`cache-purge.yml` and `stats.yml` playbooks stay in the control plane's Ansible
tree, which remains the provisioning authority.

See [PLUGINS.md](../../PLUGINS.md) for the optional-capability contract.
