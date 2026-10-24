# Edge contract tests

What the control plane emits, checked against what the edge roles under
`src/blitzecdn/ansible/roles/` declare and render. Every assertion here is
about the boundary between the two, not either side alone.

| Module | Boundary |
| --- | --- |
| `test_edge_wiring.py` | Pinned CI actions, roles the plays name, the committed desired-state fixture |
| `test_edge_keys.py` | Every emitted key is declared, every required key emitted, choices cover the enums |
| `test_edge_listeners.py` | Public ports, the default server, HTTP/3 on UDP/443 |
| `test_edge_firewall.py` | Firewall rules reaching the rendered configuration |
| `test_edge_headers.py` | The trusted `BZ-*` visitor headers on the origin leg |
| `test_edge_tls.py` | SSL modes, the origin leg, and each listener's upstream |
| `test_edge_redirects.py` | Always Use HTTPS and Under Attack Mode |
| `test_edge_features.py` | Compression, cache key, upload limits, websockets, the TLS floor |

When one of these fails, change the model and the role together in one commit.

`edge_render_support.py` holds the loaders and the Jinja environment shared
across these modules. Helpers used by one module stay beside its tests.

All application capabilities and provider fragments run together. Tests do not
skip according to package presence; feature-specific policy is set explicitly.
