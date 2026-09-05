"""The capability slices, and which of them an operator can take away.

Every directory here is one capability: its contract, and — when the control
plane could not start without it — its implementation. What a reader cannot see
from the tree is that several of these are contract *only*, because the code
that acts on the setting ships as a separate wheel. That is deliberate, and the
map is worth having in one place:

| capability | implemented by | absent without the wheel |
| --- | --- | --- |
| `sites` `dns` `edges` | itself | nothing: this is the control plane |
| `deployments` `diagnostics` `maintenance` | itself | nothing, likewise |
| `workflows` | itself | nothing, likewise |
| `cache` | `blitzecdn-cache` | purge, cache statistics |
| `compression` | `blitzecdn-compression` | gzip and Brotli on the edge |
| `security` | `blitzecdn-security` | firewall rules, Under Attack Mode |
| `security` | `blitzecdn-geoip` | country rules and the country header |
| `http` | `blitzecdn-http3` | the QUIC listener (1.1 and 2 are baseline) |
| `tls` | `blitzecdn-certificates` | issuance, renewal, upload, Automatic SSL |

Four optional distributions appear nowhere above — `blitzecdn-backup`,
`blitzecdn-hardening`, `blitzecdn-origins` and `blitzecdn-resolver` — because no
site setting asks for them. Each adds an operation, or changes what the
controller and the host do, rather than adding a property a virtual host
carries, so none has a contract here to be the other half of.

The contract stays behind when the wheel goes because a stored site has to read
back either way: a controller with `blitzecdn-cache` detached must still load a
site whose `cache_enabled` is set, and refuse the *deployment* by name through
`CapabilityPolicy.capability_requirements`, rather than fail to parse it. See
`PLUGINS.md` for the whole of that argument.
"""
