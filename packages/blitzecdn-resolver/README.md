# blitzecdn-resolver

Optional host DNS resolution for BlitzeCDN edges. Install this package to make
the `resolver` capability available.

An edge resolves names for three different reasons — apt fetches a package, the
runtime looks up an origin hostname, certificate issuance checks a CAA record —
and all three go through the host resolver. This distribution owns that
resolver where BlitzeCDN is the one who should own it, and nothing else:

| role | what it does on the host |
| --- | --- |
| `blitzecdn_resolver` | writes `/etc/systemd/resolved.conf.d/blitzecdn.conf`, restarts `systemd-resolved`, then queries a random reserved `.invalid` name and fails if the resolver answered it |
| `blitzecdn_resolver_teardown` | removes that drop-in on decommission and restarts `systemd-resolved`, so a host leaving inventory resolves through its own configuration again |

No site setting asks for this capability, so no site is ever refused for its
absence, and `blitzecdn validate` says nothing about it. Attaching or detaching
it changes no desired-state document: a fleet converges byte-identical site
configuration either way.

## It is attached by default, and off until you turn it on

`install.sh` and the container image pass `--extra resolver`, and
`blitzecdn_resolver_enabled` defaults to `false`. Both halves matter. The role
does nothing until a fleet asks for it, so attaching by default changes no
host; and a controller upgraded in place keeps managing resolution on exactly
the hosts it already was, rather than silently stopping.

Detach it when the host's DNS belongs to the network rather than to BlitzeCDN:

```bash
BLITZECDN_CAPABILITIES="backup cache hardening origins" ./install.sh
```

Turn it on where the host resolver cannot be trusted to answer public names
truthfully — a split-horizon view, an internal forwarder, or a transparent
proxy that claims every hostname:

```bash
blitzecdn config set blitzecdn_resolver_enabled true
blitzecdn config set blitzecdn_resolver_addresses '["9.9.9.9", "149.112.112.112"]'
```

Addresses only. A hostname there would have to be resolved by the resolver
being replaced, and the role refuses one.

## Why it runs where it runs

Two slots, because converging and withdrawing are two different plays.

- `edge_roles` puts `blitzecdn_resolver` **before** `blitzecdn_firewall` and
  `blitzecdn_nginx`. Everything that resolves a name afterwards depends on the
  answer, most immediately the origin hostnames the renderer writes into a
  configuration the runtime will look up.
- `teardown_roles` puts `blitzecdn_resolver_teardown` **before**
  `blitzecdn_teardown` in the decommission play. Core removes the trees it
  wrote, the shared runtime directories and every systemd unit matching the
  managed prefix; a file under `/etc/systemd/resolved.conf.d` is none of those.
  Core naming it would mean a path belonging to this wheel sitting in a role
  that is installed whether or not this wheel is — which is exactly what the
  extraction removed.

Core enforces both by position in its own plays. It never learns either role's
name.

## What the role proves before it succeeds

Replacing a host's resolver is one of the few changes BlitzeCDN makes that can
leave a working edge looking broken in ways unrelated to the CDN, so the role
refuses ambiguous input and checks the result on the host rather than trusting
that writing the file was enough:

- an enabled role with no addresses fails, rather than claiming every domain
  and answering none of them;
- a host not resolving through `systemd-resolved` fails, rather than having a
  drop-in written where nothing reads it;
- after the restart, it queries a random name under `.invalid` — reserved by
  RFC 2606 and impossible to delegate — and fails if the resolver returned an
  address for it. A resolver that invents answers makes every name-based check
  on that host compare against fiction.
