# Ansible automation

`playbooks/edge.yml` converges only the `blitzecdn_edges` inventory group. Its
focused roles own baseline packages, conservative kernel tuning, fail-closed
firewall policy, Nginx CDN sites, and SSH intrusion prevention.

All public role inputs use the `blitzecdn_*` namespace and role argument specs.
Python writes validated `blitzecdn_nginx_sites` to a restrictive file beneath
`.state/` and passes it explicitly with `--extra-vars`; generated data is never
committed or implicitly discovered.

Copy `inventory/hosts.example.yml` to the ignored `inventory/hosts.yml`. Use a
non-root `ansible_user`, an external SSH key or agent, verified `known_hosts`,
and explicit `blitzecdn_firewall_ssh_sources`. The firewall role refuses to
enable without at least one management CIDR.

Tags are `base`, `kernel`, `firewall`, `nginx`, and `security`. High-impact
deployments should normally run the complete play because firewall, port, and
Fail2Ban policy are related.

Check mode cannot prove service behavior or package availability. Always run
`blitzecdn validate`, then `blitzecdn plan`, before an applied deployment.
