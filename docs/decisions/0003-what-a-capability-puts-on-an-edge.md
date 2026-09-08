# What a capability puts on an edge host

Status: accepted; documents the design in `core.plugins.types.ansible` and
`core.plugins.resolution.ansible`.

## Context

A capability is not complete until the deployment implementation travels with
it. If a role lives in the control plane's own Ansible tree, installing or
detaching the distribution does not bring or remove the thing that converges an
edge, and the checkout becomes a second register of which capabilities exist.

Several of the things a capability needs on an edge are *global* to the host or
to the Nginx process: there is one role search path, one ordered list of roles
per play slot, and one `load_module` list per Nginx process. A package cannot
apply any of those itself without overwriting whatever another package decided.

## Decision

A package that owns a role ships the role inside its own wheel and answers
`blitzecdn_ansible_contributions` with the directory it landed in. Core adds
that directory to Ansible's role search path and learns nothing else about it.

`AnsibleContribution` has five members, and each one is there because the thing
it describes is global and therefore core's to compose. A playbook is
deliberately absent: the package that owns a play already passes its path to
`PlaybookRunner.run_playbook`.

Every role list names roles that contribution's own `roles_path` ships. Core
refuses a name that is not there, rather than letting the play fail much later
with "the role was not found". `plugin` is carried for the failure message: two
packages shipping a role of the same name is a conflict reported with both
names, not resolved by whichever registered last.

### Three slots, because there are three answers to *when*

Core owns the position; a capability declares which slot it wants and never the
ordering. Each slot is one `blitzecdn_capabilities` role invocation taking the
composed list as an extra-var, so the plays name no capability and need no edit
when one is attached or detached. The slots are opposites rather than
preferences.

**`edge_roles`** run after the host has an engine, a runtime image and its
persistent directories, and *before* the firewall opens a port or
`blitzecdn_nginx` renders and validates the configuration tree. This is where a
capability contributing something the configuration then depends on has to be —
a lookup database, an njs module, a snippet in `conf.d` — because all of it must
exist before `nginx -t` decides whether the tree may be served.

**`host_roles`** run at the end, after `blitzecdn_edge_stack` has the edge
serving. This is where a capability configuring the *host underneath* the
runtime has to be, and SSH hardening shows why it cannot be the earlier slot.
It must come after the firewall, because a host that fails firewall validation
must never be left key-only and unreachable from the management network; and
after `blitzecdn_edge_stack`, because an edge whose containers are all broken
must still be reachable for Ansible to repair it — so nothing that could close
the management path may run before the runtime has proved it serves.

Ordering *within* a slot is the contributing package's, which is how a Fail2Ban
jail follows SSH policy without the play knowing either exists. A role here
reads nothing the renderer produced and contributes nothing it reads.

**`teardown_roles`** run in the decommission play, before
`blitzecdn_edge_teardown`. A capability that put something on a host must be
able to take it off again, and core cannot do it on the capability's behalf:
that would mean naming a path belonging to a wheel that may not be installed, in
a role that always is. A host is also usually decommissioned by a controller
whose package set has drifted from the one that converged it, so "the capability
is still attached" is not something removal may depend on. What core owns is the
position — running before `blitzecdn_edge_teardown` keeps that role's clean-host
assertion the last word on the whole decommission, rather than a verdict passed
before half the removal happened.

### Nginx dynamic modules are declared, not built in

A dynamic module has two halves answered in two different places. It has to be
*in the image* — built against that exact Nginx ABI, or already shipped by the
official base — and it has to be *loaded* by the running configuration, which is
one `load_module` list the whole process shares.

Written into the edge image's own build context, both halves would make that
build a third register of which capabilities exist, and the only one that keeps
naming a capability after its distribution is detached: an image is built once
and pinned by digest, so an edge with no `blitzecdn-geoip` would still load the
GeoIP2 module.

`EdgeModule` answers both from one place. Core composes the resolved set into the
`load_module` list `blitzecdn_nginx` renders onto the host, so an edge loads
exactly what is installed; and `blitzecdn edge image spec` emits the same set as
the build arguments the image is built from, so the Dockerfile enumerates
nothing.

Three fields carry what core cannot infer:

- `objects` are the shared objects to load, in order. There may be more than
  one: `brotli` builds a filter and a static module, and only the filter is
  loaded. Core supplies the directory; a capability names the files.
- `build` distinguishes a module the image must compile from one the official
  base already carries. njs is the second kind, and it cannot be inferred from
  the name: asking pkg-oss to build a module that is already installed is a
  build failure, and omitting one that is not is an edge that fails `nginx -t`
  on its first deploy.
- `probe` is one directive the module registers, evaluated by the image's
  build-time probe. A module that loads but registers nothing is
  indistinguishable from a working one until an edge configuration uses it,
  which is far too late — and only the capability knows which directive is its.

A module whose every directive requires a data file the image must not carry can
omit `probe`; `load_module` in the probe still proves it loads.

## Consequences

Detaching a distribution removes its roles from Ansible's search path and from
the play in the same act. Core's plays name no capability's role.

The edge image enumerates no capabilities. Its Dockerfile takes the module set
as build arguments, so rebuilding after attaching a capability is a build-arg
change rather than an edit.

What a capability asks an operator to *configure* is not on this contract, even
though secrets are forwarded into Ansible's subprocess environment. That belongs
to `ConfigurationContribution` —
[0002](0002-capability-configuration-ownership.md).

## Code and verification

- [Contribution types](../../src/blitzecdn/core/plugins/types/ansible.py)
- [Role and slot resolution](../../src/blitzecdn/core/plugins/resolution/ansible.py)
- [Module resolution](../../src/blitzecdn/core/plugins/resolution/modules.py)
- [Image paths and the probe frame](../../src/blitzecdn/docker/__init__.py)
- [A host-slot capability](../../packages/blitzecdn-hardening/src/blitzecdn_hardening/plugin.py)
- [A module-contributing capability](../../packages/blitzecdn-geoip/src/blitzecdn_geoip/plugin.py)
- [Contribution tests](../../tests/contract/test_ansible_contributions.py)
- [Role contract tests](../../tests/contract/roles/)
