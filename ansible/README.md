# Operator Ansible configuration

This directory is the operator-side half of the deployment: inventory, group
variables, connection settings, and the playbooks the control plane invokes.
The roles are **not** here — they ship in the `blitzecdn.edge` collection,
pinned in [`requirements.yml`](requirements.yml).

```bash
ansible-galaxy collection install -r requirements.yml -p ../.state/collections
```

| Path | Purpose |
| --- | --- |
| `requirements.yml` | Pins the exact `blitzecdn.edge` version this controller deploys |
| `inventory/hosts.example.yml` | Template for the ignored `inventory/hosts.yml` |
| `inventory/group_vars/` | Environment policy for `blitzecdn_edges` |
| `playbooks/edge.yml` | Converges edges by calling the collection's roles by FQCN |
| `playbooks/acme-challenge.yml` | Publishes an HTTP-01 token to every edge |
| `ansible.cfg` | Connection, fork, and collection-path settings |

`playbooks/edge.yml` converges only the `blitzecdn_edges` inventory group. Tags
are `base`, `kernel`, `firewall`, `nginx`, and `security`; high-impact
deployments should normally run the complete play because firewall, port, and
Fail2Ban policy are related.

The control plane writes validated `blitzecdn_nginx_sites` to a restrictive
file beneath `.state/` and passes it explicitly with `--extra-vars`. Generated
data is never committed or implicitly discovered.

Copy `inventory/hosts.example.yml` to the ignored `inventory/hosts.yml`. Use a
non-root `ansible_user`, an external SSH key or agent, verified `known_hosts`,
and explicit `blitzecdn_firewall_ssh_sources` — the firewall role refuses to
enable without at least one management CIDR.

Group variables must live in `inventory/group_vars/`, beside the inventory
file. Ansible only auto-loads `group_vars` adjacent to the inventory or the
playbook, so a `group_vars/` directory anywhere else is silently ignored and
every setting in it falls back to the role default.

Check mode cannot prove service behaviour or package availability. Always run
`blitzecdn validate`, then `blitzecdn plan`, before an applied deployment.
