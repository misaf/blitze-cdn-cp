# Ansible quick start

The control plane invokes the playbooks and roles in this directory to manage
BlitzeCDN edges. They live *inside* the `blitzecdn` package, so they ship in the
wheel and resolve identically in a checkout and on a controller that has no
checkout — the same way every optional capability carries its own Ansible. There is no host inventory file to edit: the inventory plugin
reads registered edges and fleet configuration from the control-plane database.

Install the controller from the project root, then register and deploy an edge:

```bash
BLITZECDN_DEV=1 ./install.sh

blitzecdn edge add edge-01 \
  --host 192.0.2.10 \
  --public-address 203.0.113.10 \
  --user deploy \
  --ssh-source 198.51.100.0/24
blitzecdn validate
blitzecdn plan
blitzecdn deploy
```

Use a non-root SSH user, verified host keys, and at least one trusted management
CIDR. Managed edges must be fresh Ubuntu 26.04 LTS hosts.

Inspect the generated inventory when troubleshooting:

```bash
ANSIBLE_CONFIG=src/blitzecdn/ansible/ansible.cfg \
  ansible-inventory --list
```

The main entry points are:

- `inventory/blitzecdn.yml` — dynamic inventory configuration
- `plugins/inventory/blitzecdn.py` — database-backed inventory plugin
- `inventory/group_vars/` — shipped fleet defaults
- `playbooks/edge.yml` — edge convergence
- `playbooks/decommission.yml` — managed edge removal
- `playbooks/uninstall.yml` — standalone host removal
- `roles/` — controller and edge roles
- `roles/blitzecdn_edge/` — the shared edge runtime contract
- `roles/blitzecdn_capabilities/` — the slot an installed capability's role fills

Do not edit tracked fleet defaults for local overrides. Use
`blitzecdn config set`, `blitzecdn config list`, and `blitzecdn config unset`.

## The edge runtime contract

`blitzecdn_nginx`, `blitzecdn_edge_stack` and `blitzecdn_firewall` converge the
same machine and need the same answers about it. Those answers are one
variable, `blitzecdn_edge_runtime`, declared and validated by the
`blitzecdn_edge` role, which `playbooks/edge.yml` runs first:

```yaml
blitzecdn_edge_runtime:
  container:  blitzecdn-edge
  image:      "{{ blitzecdn_edge_image }}"
  paths:      {state, nginx, modules, tls, cache, logs, acme, empty, data}
  status:     {address, port, path}
  listeners:  {http: [...], https: [...], http3: false}
```

`paths.data` and `paths.modules` are the two members that exist for someone
else. Core creates both and mounts them read-only into the edge container and
into the configuration test; what goes in them — a lookup database, an njs
module — is an installed capability's business, written by that capability's
own role. There is deliberately no `geoip` member any more: the shared contract
every edge role reads carried the name of a distribution that may not be
installed.

The three roles read it and never each other. A value only one role uses stays
in that role — Nginx policy in `blitzecdn_nginx`, the Compose project and the
health timeout in `blitzecdn_edge_stack` — and `tests/
test_ansible_role_contracts.py` fails a change that breaks either rule.

Members that are not fixed layout are composed from flat inputs, because
desired state and `blitzecdn config set` reach Ansible as top-level variables
and neither can override one member of a dictionary:

| Input | Set by | Becomes |
| --- | --- | --- |
| `blitzecdn_edge_image` | `blitzecdn config set` | `.image` |
| `blitzecdn_edge_http3_enabled` | desired state | `.listeners.http3` |

## What is in this tree, and what is not

This tree is the *platform*: the base host, the kernel, Docker, the firewall,
the edge runtime contract, the edge stack, and `blitzecdn_nginx`, which renders
a site's whole configuration from the merged desired-state document.

An optional capability's roles are not here. They ship inside that
capability's wheel — `blitzecdn_geoip` in `blitzecdn-geoip`, `blitzecdn_cache`
and `blitzecdn_cache_stats` in `blitzecdn-cache`, `blitzecdn_hardening_sshd`,
`blitzecdn_hardening_fail2ban` and `blitzecdn_hardening_teardown` in
`blitzecdn-hardening`, `blitzecdn_resolver` and
`blitzecdn_resolver_teardown` in `blitzecdn-resolver`, and so on — and the control plane
composes the real role search path from core's directory plus the directory
each installed plugin reports. `roles_path` in `ansible.cfg` is only what a
bare `ansible-playbook` in a checkout resolves against; every run the control
plane makes overrides it with `ANSIBLE_ROLES_PATH`.

`roles/blitzecdn_capabilities` is how a contributed role actually runs. It
names no capability: the control plane passes the slot's list as an extra-var
on every run, composed from the installed plugins, and the role includes each
one. There are three slots, and each position is a contract:

| slot | extra-var | where | for |
| --- | --- | --- | --- |
| edge | `blitzecdn_capability_roles` | between `blitzecdn_kernel` and `blitzecdn_firewall` in `playbooks/edge.yml` | a capability contributing something the rendered configuration then depends on — early enough to have the container engine and the persistent directories, late enough that `blitzecdn_nginx` proves the whole tree loads afterwards |
| host | `blitzecdn_host_capability_roles` | after `blitzecdn_edge_stack` in `playbooks/edge.yml` | a capability configuring the host underneath a runtime that is already serving; an edge whose containers are all broken must still be reachable for Ansible to repair it |
| teardown | `blitzecdn_teardown_capability_roles` | before `blitzecdn_teardown` in `playbooks/decommission.yml` | a capability withdrawing what it wrote from a host that is leaving inventory, while the state tree is still there and before core's clean-host assertion passes the verdict |

To run the edge play by hand against a fleet with capabilities attached, ask
the control plane for the roles path and the slot lists rather than writing
either out. Both are composed by the functions the composition root itself
calls, so a play run this way resolves exactly what a deployment would:

```bash
ANSIBLE_CONFIG=src/blitzecdn/ansible/ansible.cfg \
ANSIBLE_LOCAL_TEMP=.state/ansible-local \
ANSIBLE_ROLES_PATH="$(blitzecdn ansible roles-path)" \
  ansible-playbook src/blitzecdn/ansible/playbooks/edge.yml \
  --extra-vars "$(blitzecdn ansible slots)"
```

Which slot a role belongs to is declared by the package that ships it and
cannot be worked out by reading this repository, which is why writing the list
by hand is the one part of this that reliably goes wrong: the justfile's copy
had drifted in both directions before these commands existed — one edge role
missing, and the teardown slot never passed at all.

`just ansible-check` does exactly this for the syntax and lint gates.

From the project root, run the Ansible checks with:

```bash
just ansible-check
```

See the project [quick start](../README.md) and the
[full documentation](https://github.com/misaf/blitze-cdn-web) for installation,
configuration, upgrades, rollback, and recovery.
