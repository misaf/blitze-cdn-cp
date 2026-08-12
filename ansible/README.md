# Operator Ansible configuration

This directory is the operator-side half of the deployment: the inventory
plugin, group variables, connection settings, and the playbooks the control
plane invokes.

**There is no inventory file to edit.** The fleet lives in the `edges` table of
the control-plane database, and `plugins/inventory/blitzecdn.py` reads it at the
start of every run — so a host exists for Ansible exactly when it exists in the
control plane, with nothing in between to drift. Manage it with `blitzecdn edge
add`, `edge update`, `edge list` and `edge remove`.

The roles are **not** here — they ship in the `blitzecdn.edge` collection,
pinned in [`requirements.yml`](requirements.yml).

```bash
ansible-galaxy collection install -r requirements.yml -p ../.state/collections
```

| Path | Purpose |
| --- | --- |
| `requirements.yml` | Pins the exact `blitzecdn.edge` version this controller deploys |
| `inventory/blitzecdn.yml` | The inventory: configuration for the plugin below, not a host list |
| `plugins/inventory/blitzecdn.py` | Publishes the `edges` table into the `blitzecdn_edges` group |
| `inventory/group_vars/` | Environment policy for `blitzecdn_edges` |
| `playbooks/edge.yml` | Converges edges by calling the collection's roles by FQCN |
| `playbooks/acme-challenge.yml` | Publishes an HTTP-01 token to every edge |
| `playbooks/cache-purge.yml` | Removes cached responses by key, or empties the cache |
| `playbooks/stats.yml` | Collects cache and connection counters from every edge |
| `playbooks/decommission.yml` | Takes a host out of service and removes managed state |
| `ansible.cfg` | Connection, fork, and collection-path settings |

`playbooks/edge.yml` converges only the `blitzecdn_edges` inventory group. Tags
are `base`, `resolver`, `kernel`, `firewall`, `nginx`, `sshd`, and `security`
(`sshd` and Fail2Ban both carry `security`). High-impact deployments should
normally run the complete play because firewall, port, and Fail2Ban policy are
related.

The control plane writes validated `blitzecdn_nginx_sites` to a restrictive
file beneath `.state/` and passes it explicitly with `--extra-vars`. Generated
data is never committed or implicitly discovered.

## The inventory

Register an edge through the control plane; there is nothing to copy or edit:

```bash
blitzecdn edge add edge-01 --host 192.0.2.10 --port 22 \
    --ssh-source 198.51.100.0/24 --public-address 203.0.113.10
```

To see exactly what Ansible will be given, including the group variables that
apply on top:

```bash
ansible-inventory -i inventory/blitzecdn.yml --list
```

The plugin finds the database from `BLITZE_DATABASE_PATH`, which the control
plane exports for every run it starts, and otherwise falls back to
`../.state/control-plane.db` — so the command above works as written on a
default install. A database that predates this layout has no `edges` table and
the plugin says so; `blitzecdn setup` creates it and imports any existing
`inventory/hosts.yml`, renaming that file to `hosts.yml.migrated` once it has.

Use a non-root `--user`, an external SSH key or agent, verified `known_hosts`,
and at least one `--ssh-source` management CIDR — the firewall role refuses to
enable without one.

`--ssh-source` and `--port` are published as **host** variables. Both used to be
set in `group_vars/blitzecdn_edges/`, which meant two things that hurt: an
inventory group variable loses to a `group_vars/` file, so `edge add
--ssh-source` recorded a value the firewall never applied; and the firewall port
was maintained separately from `ansible_port`, so a mismatch closed the port the
next converge arrived on. Both now come from the edge itself and cannot
disagree. Setting either key in `group_vars/` has no effect.

Group variables must live in `inventory/group_vars/`, beside the inventory
source. Ansible only auto-loads `group_vars` adjacent to the inventory or the
playbook, so a `group_vars/` directory anywhere else is silently ignored and
every setting in it falls back to the role default. This did not change with
the move to a plugin: the directory is loaded for the group the plugin creates
exactly as it was for the group the old static file declared.

Check mode cannot prove service behaviour or package availability. Always run
`blitzecdn validate`, then `blitzecdn plan`, before an applied deployment.
