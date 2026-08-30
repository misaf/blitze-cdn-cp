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

Do not edit tracked fleet defaults for local overrides. Use
`blitzecdn config set`, `blitzecdn config list`, and `blitzecdn config unset`.

From the project root, run the Ansible checks with:

```bash
just ansible-check
```

See the project [quick start](../README.md) and the
[full documentation](https://github.com/misaf/blitze-cdn-web) for installation,
configuration, upgrades, rollback, and recovery.
