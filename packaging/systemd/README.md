# Scheduled control-plane runs

The API service owns first-certificate reconciliation while it runs. The renewal
and drift timers are also safe unattended: renewal leaves anything not yet due
alone and deploys only after renewing at least one certificate; drift exits `6`
so a scheduler can alert on it. None is enabled automatically.

Both units assume the control plane runs as an unprivileged `blitzecdn` account
out of `/opt/blitzecdn`. It needs no privilege on the controller — everything it
changes remotely goes over SSH as the edge user.

## Install

If your layout differs, change `User`, `WorkingDirectory`, `ReadWritePaths`, and
`ExecStart` **together**. `ProtectSystem=strict` makes the whole filesystem
read-only except what `ReadWritePaths` lists, so a path you forget is not a
permissions warning — it is a run that fails on its first write.

```bash
sudo cp packaging/systemd/blitzecdn-*.service /etc/systemd/system/
sudo cp packaging/systemd/blitzecdn-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blitzecdn-api.service
sudo systemctl enable --now blitzecdn-cert-renew.timer
sudo systemctl enable --now blitzecdn-drift.timer
```

Verify before trusting the schedule — a timer that never fires looks exactly
like a fleet that never drifts:

```bash
systemctl list-timers 'blitzecdn-*'
sudo systemctl start blitzecdn-drift.service   # run once, now
journalctl -u blitzecdn-drift.service -n 50
```

## Reading the results

The packaged renewal service uses `cert renew --deploy`: it installs renewed
material on the edge fleet in the same run and fails if either renewal or that
deployment fails. Interactive renewal without `--deploy` still updates only the
controller store.

`blitzecdn drift` exits `6` when a reachable edge no longer matches desired
state. `blitzecdn-drift.service` lists that as a success status on purpose: it
is a real answer, not a malfunction. Marking it `failed` would bury a check that
genuinely broke among all the ones that merely found drift.

So alert on the exit code, not on unit failure:

```bash
journalctl -u blitzecdn-drift.service -o json --since -1d \
  | grep -c '"EXIT_STATUS":"6"'
```

To be paged when a unit genuinely breaks — the check could not run at all — add
a drop-in with `OnFailure=` pointing at your notification unit. That fires for
breakage, not for drift.

## What is deliberately not scheduled

`blitzecdn deploy` changes what the edges serve and stays manual. An automatic
apply would turn one bad record into a fleet-wide outage with nobody watching.
Drift tells you the fleet moved; converging it is still a decision.

`blitzecdn cache purge` is likewise on demand. A scheduled purge is a scheduled
origin traffic spike.
