# Edge capacity and kernel settings

Edge capacity is controlled at several layers. The defaults are:

| Setting | Default | Scope |
| --- | --- | --- |
| `blitzecdn_edge_nofile` | 65536 | Soft and hard container limits, Nginx worker limit, and configuration-test container |
| `blitzecdn_nginx_worker_connections` | 8192 | Connections per worker, including connections to origins |
| `blitzecdn_nginx_listen_backlog` | 16384 | Public TCP listener backlog |
| `net.core.somaxconn` | 16384 | Host ceiling for the listener backlog |
| `net.ipv4.tcp_max_syn_backlog` | 16384 | Pending TCP handshakes |
| `net.core.netdev_max_backlog` | 16384 | Input packet backlog |
| `net.ipv4.ip_local_port_range` | 32768–65535 | Automatic outgoing source ports |

`worker_connections` must remain below `nofile`, leaving descriptors for logs,
cache files, and listeners. A proxied request can use both a visitor connection
and an origin connection. Raising a connection setting does not guarantee that
CPU, memory, network bandwidth, or origin capacity can sustain the resulting
traffic. Changing the container limit recreates the container on converge.

The host must support the requested process limit through `fs.nr_open` and have
sufficient system-wide file capacity through `fs.file-max`. BlitzeCDN does not
replace these with fixed values that could lower a host's existing capacity.
Measure `fs.file-nr` and worker `/proc/<pid>/limits` on the actual Linux edge.

The kernel role runs on the host; the serving container uses host networking.
Do not move these network sysctls into Compose. Linux also applies
`ip_local_reserved_ports` when selecting automatic source ports. When using
custom listeners or co-located services, review that reservation list and the
source-port range together.

## Overrides and BBR

`blitzecdn_kernel_sysctl` replaces the entire default dictionary. Restate the
settings you want to retain. TCP keepalive defaults remain 90 seconds idle,
30 seconds between probes, and five probes; sockets must enable TCP keepalive
for those defaults to matter.

BBR and `fq` are opt-in. For example, add these entries to the full dictionary:

```yaml
net.core.default_qdisc: fq
net.ipv4.tcp_congestion_control: bbr
```

The role checks registered congestion algorithms first. If BBR is requested but
absent, it attempts `modprobe tcp_bbr`, then checks registration again before
changing sysctls. Built-in or already loaded BBR needs no module-loading step.
An unsupported selection fails the converge. Check mode does not load modules:
load BBR before using check mode to validate that selection.

Changing `default_qdisc` does not replace an existing interface's queueing
discipline. Inspect `tc qdisc show` and verify the effective policy after a
reboot. Keep the distribution's `tcp_tw_reuse` and `tcp_fin_timeout` policies
unless measurements justify changing them. The latter controls orphaned
`FIN_WAIT_2` sockets, rather than `TIME_WAIT` duration.

## Withdrawing tuning

Before changing a key, the kernel role saves its current value in
`/etc/sysctl.d/60-blitzecdn.conf.baseline.json` (alongside a custom drop-in path
when configured). Removing that key from inventory restores its saved value
and removes its entry from the managed drop-in. An empty dictionary restores
all saved values. Once restored, the key is released from the baseline so a
later addition captures its then-current value.

The snapshot records values observed when this implementation first takes
ownership. On an existing edge, that can include tuning from an older release;
it cannot reconstruct values from before that installation. Existing stale
entries are pruned from the file, but values with no saved baseline cannot be
restored historically. Review migrated hosts explicitly.

Decommission restores the saved baseline before deleting the drop-in and its
snapshot. Retain the snapshot until tuning has been withdrawn. A reboot or
another sysctl manager may subsequently apply a different policy; the snapshot
is an observed baseline, not a distribution-default database.

Verify convergence twice, effective worker limits and listener queues, kernel
settings after reboot, and representative cache-hit and origin-heavy loads on
a supported Ubuntu edge before treating capacity as established.
