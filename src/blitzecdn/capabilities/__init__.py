"""The capability slices, and which of them an operator can take away.

Every directory here is one capability: its contract, and — when the control
plane could not start without it — its implementation. What a reader cannot see
from the tree is that several of these are contract *only*, because the code
that acts on the setting ships as a separate wheel. That is deliberate, and the
map is worth having in one place:

| capability | implemented by | absent without the wheel |
| --- | --- | --- |
| `dns` `edges` `releases` `jobs` | itself | nothing: this is the control plane |
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

A capability reaches the control plane by being named in
`composition.control_plane.BUILTIN_PLUGINS`, which imports its `plugin.py` and
hands the hookimpls to Pluggy. Eight of these eleven directories hold one, and
the split is not the one the table above describes: `http` and `tls` are
contract capabilities that register anyway, while `cache`, `compression` and
`security` do not register at all.

What decides it is who else claims the name. A plugin name is unique across
everything installed — discovery refuses two plugins answering to one — and
`capability_requirements` is written in those names: a zone with `cache_enabled`
requires `cache`, one with `under_attack_mode` requires `security`. The wheels
that implement those three register under exactly those names, so core cannot
also register them; the requirement is satisfied by the wheel's presence and
unsatisfiable without it, which is the whole mechanism.

`tls` and `http` are not in that position. Their wheels are named
`certificates` and `http3`, so `tls` and `http` are free for core to claim, and
claiming them is worth something: a name no plugin holds cannot be resolved to
a version, a summary, or an answer to "is it installed?". `tls` registers
metadata and nothing else for that reason alone.

So an absent `plugin.py` is not an omission to correct. It says this directory
is a policy class and nothing more — imported by `dns`, which composes it onto
the zone, and composition is an import rather than a hook.

The contract stays behind when the wheel goes because a stored zone has to read
back either way: a controller with `blitzecdn-cache` detached must still load a
zone whose `cache_enabled` is set, and refuse the *deployment* by name through
`CapabilityPolicy.capability_requirements`, rather than fail to parse it. See
`PLUGINS.md` for the whole of that argument.
"""
