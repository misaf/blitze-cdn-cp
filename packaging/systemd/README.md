# Control-plane service

The API service uses APScheduler only to publish certificate reconciliation,
renewal, and drift jobs. All scheduled jobs, deployments, and rollbacks are
sent through Redis to the Dramatiq worker. systemd owns the process lifecycle:
starting at boot, restarting after failure, privileges, and logs.

Both units assume the control plane runs as an unprivileged `blitzecdn` account
out of `/opt/blitzecdn`. It needs no privilege on the controller — everything it
changes remotely goes over SSH as the edge user.

## Install

If your layout differs, change `User`, `WorkingDirectory`, `ReadWritePaths`, and
`ExecStart` **together**. `ProtectSystem=strict` makes the whole filesystem
read-only except what `ReadWritePaths` lists, so a path you forget is not a
permissions warning — it is a run that fails on its first write.

```bash
sudo cp packaging/systemd/blitzecdn-api.service /etc/systemd/system/
sudo cp packaging/systemd/blitzecdn-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blitzecdn-api.service
sudo systemctl enable --now blitzecdn-worker.service
```

Verify both services and inspect scheduling and worker execution separately:

```bash
journalctl -u blitzecdn-api.service -n 100
journalctl -u blitzecdn-worker.service -n 100
```

Set an interval to `0` to disable its job. Renewal defaults to 12 hours, drift
to one hour, and first-certificate reconciliation to ten minutes. Scheduler
publication failures are written to the API journal; Dramatiq execution and
job failures are written to the worker journal.

## What is deliberately not scheduled

`blitzecdn deploy` changes what the edges serve and stays manual. An automatic
apply would turn one bad record into a fleet-wide outage with nobody watching.
Drift tells you the fleet moved; converging it is still a decision.

`blitzecdn cache purge` is likewise on demand. A scheduled purge is a scheduled
origin traffic spike.
