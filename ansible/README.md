# Ansible quick start

The control plane invokes the playbooks and roles in this directory to manage
BlitzeCDN edges. There is no host inventory file to edit: the inventory plugin
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
ANSIBLE_CONFIG=ansible/ansible.cfg \
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
  paths:      {state, nginx, modules, tls, cache, logs, acme, empty}
  status:     {address, port, path}
  listeners:  {http: [...], https: [...], http3: false}
  geoip:      {enabled, database, directory}
```

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
| `blitzecdn_edge_geoip_enabled` | fleet settings | `.geoip.enabled` |
| `blitzecdn_edge_geoip_database` | fleet settings | `.geoip.database`, `.geoip.directory` |

From the project root, run the Ansible checks with:

```bash
just ansible-check
```

See the project [quick start](../README.md) and the
[full documentation](https://github.com/misaf/blitze-cdn-web) for installation,
configuration, upgrades, rollback, and recovery.
