# blitzecdn-hardening

Optional host-access hardening for BlitzeCDN edges. Install this package to
make the `hardening` capability available.

An edge is two machines in one: the containerised runtime that serves visitor
traffic, and the Ubuntu host underneath it that the control plane reaches over
SSH. This distribution owns the second one's front door, and nothing else:

| role | what it does on the host |
| --- | --- |
| `blitzecdn_hardening_sshd` | writes `/etc/ssh/sshd_config.d/50-blitzecdn.conf`, disables password and keyboard-interactive authentication, then reads the effective policy back with `sshd -T` and fails if it did not take |
| `blitzecdn_hardening_fail2ban` | installs Fail2Ban and writes the `sshd` jail at `/etc/fail2ban/jail.d/blitzecdn-sshd.local` |
| `blitzecdn_hardening_teardown` | on decommission, removes both files in the reverse order and settles both services, so a host leaving inventory governs its own SSH access again |

The first two run in the edge play's *host* slot, after the firewall has been
validated and the runtime is serving; the third runs in the decommission play's
slot, before `blitzecdn_teardown` passes its verdict on the host. Core used to
carry those two paths in `blitzecdn_teardown`'s own defaults and reload both
services from its own handlers, which put a capability's paths in a role that
is installed whether or not the capability is — and left a fleet that had
detached this distribution with a decommission asserting against files nothing
on its controller could write.

No site setting asks for this capability, so no site is ever refused for its
absence, and `blitzecdn validate` says nothing about it. Attaching or detaching
it changes no desired-state document: a fleet converges byte-identical site
configuration either way.

## It is attached by default

`install.sh` and the container image pass `--extra hardening`, so an ordinary
installation hardens its edges. Detach it only when something else owns host
access:

```bash
BLITZECDN_CAPABILITIES="backup cache" ./install.sh
```

That is the whole point of the extraction. A fleet whose `sshd_config` belongs
to a golden image, to a configuration-management tool that predates BlitzeCDN,
or to a bastion host had no way to say so while these roles were part of the
control plane's own edge play — the play named them, so every converge rewrote
both files. Detaching is now how a fleet declines, and an operator who wants
BlitzeCDN to stay out of `/etc/ssh` does not have to fork a playbook to get it.

## Why it runs where it runs

Contributed through `host_roles`, not `edge_roles`. The two slots are not a
preference:

- `edge_roles` run **before** `blitzecdn_firewall` and `blitzecdn_nginx`,
  because what those capabilities put on an edge — a GeoIP2 database, an njs
  module, a snippet in `conf.d` — is something the rendered configuration then
  depends on, and `nginx -t` has to see it.
- `host_roles` run **after** `blitzecdn_edge_stack`, because SSH policy must
  follow firewall validation (a host that fails validation must never be left
  key-only *and* unreachable from the management network) and Fail2Ban must
  follow SSH (so its jail protects a daemon that has already stopped accepting
  passwords).

Core enforces both by position in its own play. It never learns either role's
name.

## Fleet settings

Non-secret policy lives in each role's own `defaults/main.yml` and is overridden
the way all fleet policy is:

```bash
blitzecdn config set blitzecdn_hardening_sshd_allow_users '["deploy"]'
blitzecdn config set blitzecdn_hardening_fail2ban_max_retry 3
```

`blitzecdn_hardening_sshd` will not disable password authentication until it has proved,
on the host itself, that every account it is about to restrict already holds a
usable public key — including the ownership and mode checks `StrictModes`
applies. A host reached by password fails the play instead of being locked out
by it.
