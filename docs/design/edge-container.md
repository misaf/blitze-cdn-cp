# Edge container

Code: `assets/ansible/roles/blitzecdn_edge_stack/templates/compose.yml.j2`.

The Compose project holds the whole edge runtime. Everything the operating
system owns (SSH, the firewall, sysctl, Docker itself) stays outside it, so a
broken stack still leaves a host Ansible can reach and repair.

## Host networking

The edge listens on many public TCP ports plus UDP/443, and each must see the
visitor's real address: `$remote_addr` feeds `BZ-Connecting-IP`, any country
lookup and every per-site source rule. Behind Docker's bridge and userland
proxy it would be a gateway address, and a country rule would match the edge
itself. Host networking also removes a NAT hop per packet and a publish list to
keep in step with the contract's listener sets.

## Privileges

`no-new-privileges` stops any process gaining privileges it was not started
with (setuid binaries, file capabilities). The nginx master starts as root to
bind privileged ports and read private keys, then drops to the image's own
user, which is a reduction and unaffected. That user is Alpine nginx's
`nginx`; its uid is `blitzecdn_edge_runtime.worker_uid`, and host directories
the worker reads are chowned to it.

`init: true` reaps the zombies an nginx master would otherwise leave as PID 1.

## Shutdown

`stop_signal: SIGQUIT` is nginx's graceful shutdown. SIGTERM is immediate and
turns every container replacement into a burst of visitor errors.

## Mounts

Everything is read-only except the cache and the logs. The edge has no
business rewriting what the control plane rendered, and a container that
cannot write its configuration cannot persist a change the next converge
would silently revert.

* **Configuration releases**, mounted as one stable parent: activation swaps
  the `current` link inside it, which Docker only sees because the mount is
  the parent.
* **TLS**: the host keeps the directory 0700 root and each key 0600; only the
  master reads them, at start and on reload.
* **ACME webroot**, written by the acme-challenge playbook on the host.
* **Empty document root** that Under Attack Mode dispatches through.
* **Capability data**: always mounted, so the file has one shape everywhere,
  and read-only because the process maintaining it runs on the host.
* **Secrets**: capability credentials, outside every release.
* **Cache** (writable): refilling from origins after every upgrade is an
  outage in slow motion.
* **Logs** (writable): the record of what the edge did, read by traffic
  accounting and cache statistics.

## Logging and health

Container stdout/stderr is bounded here as well as in `daemon.json`, so the
stack carries its own limit onto an engine configured by someone else.
Request logs are the bind-mounted files and are unaffected.

The healthcheck probes the loopback status endpoint: can nginx serve a request
right now? A failing probe marks the container unhealthy but does not restart
it. Deep deployment checks live in `health.yml`.
