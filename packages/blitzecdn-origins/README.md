# blitzecdn-origins

Optional fleet origin-probing capability for BlitzeCDN. Install this package to
make the `origins` capability available.

"Can the edges reach the origins they proxy to?" is an *operation*: it takes no
deployment lock, changes nothing on any host, and answers a question about the
world rather than about configuration. It ships as a wheel for the same reason
purging a cache and issuing a certificate do.

| what it adds | |
| --- | --- |
| `blitzecdn origin check` | exits 3 if any edge could not reach an origin |
| `POST /v1/origins/check`, `POST /v2/origins/check` | the same report over HTTP |
| `blitzecdn_origins` role + `origin-check.yml` play | shipped inside the wheel and located with `importlib.resources` |

## The edges answer, not the controller

The check used to run on the control plane, and the answer it gave was about
the *controller's* network — its routes, its resolver, its egress firewall. An
origin that allow-lists the edges refuses the controller while working
perfectly; one reachable only from the controller's subnet passes and then 502s
on every edge. So each edge probes for itself and reports back through the
`blitzecdn_report` channel, and the report is per edge **and** per site:

- an origin no edge can reach is **down**;
- an origin only some edges can reach is a **routing or allow-list problem**;

and a single vantage point could never have told those apart.

## It converges nothing

The Ansible contribution declares a role search path and neither slot of core's
edge play. The role is reached only by this package's own play, on demand, so a
deploy converges byte-identical desired state whether or not this package is
attached — and no site setting asks for the `origins` token, so no site is ever
refused for its absence.

What core keeps is `OriginCheck`, the single-origin row. The controller's own
advisory probe inside certificate preflight produces one without running this
play, and it has to answer in milliseconds during issuance.

## blitzecdn-certificates depends on this

Automatic SSL/TLS probes every candidate origin over its current transport and
again under Full (strict), and upgrades only where every edge agrees. That is
this capability's play, so `blitzecdn-certificates` declares
`blitzecdn-origins` as a real dependency — pip installs both, and detaching
this package cannot leave the scan importing something that is gone.

It is the workspace's one declared optional-to-optional edge, and it is
declared precisely so that it is not an import that merely happens to work.
Before this package existed, the report parser lived in two places; there is
one copy now, in `reporting.py`.
