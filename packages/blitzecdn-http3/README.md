# blitzecdn-http3

Optional HTTP/3-over-QUIC implementation for BlitzeCDN. Install this package to
make the `http3` capability available.

HTTP/1.1 and HTTP/2 are baseline: every managed edge serves them, and nothing
needs to be installed for that. The stable `ProtocolPolicy` — including
`http3_enabled` — remains in `blitzecdn`, so a site that asks for HTTP/3 still
loads when this package is absent.

| This package | `http3_enabled` | Result |
| --- | --- | --- |
| absent | `false` | Site is served over HTTP/1.1 and HTTP/2. Nothing is missing. |
| absent | `true` | Deployment is refused at validation, naming the `http3` capability. The site is never silently downgraded. |
| installed | `true` | The fleet opens a QUIC listener and one site is named to carry `reuseport`. |

Attach it in a checkout with `uv sync --extra http3`, or in an installation by
adding `http3` to `BLITZECDN_CAPABILITIES`.

The Ansible roles remain the provisioning authority: the Nginx directives, the
QUIC listener, the UDP firewall rule and the Nginx module capability probe all
stay in `blitzecdn_edge`, `blitzecdn_nginx` and `blitzecdn_firewall`. What this
package owns is whether the control plane offers the capability at all, the two
fleet variables that describe the listener, and the validation that refuses a
site asking for HTTP/3 without it.
