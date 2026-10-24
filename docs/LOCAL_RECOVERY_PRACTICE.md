# Local recovery practice

Step 2 of the operating and recovery work. Run on 2026-10-24 from the
`8ce5f08` checkpoint, with the new tests in this step. Application behavior was
not changed. All controller directories, archives, credentials and certificate
material were disposable test data.

## What happened

| Practice | Observed result |
| --- | --- |
| Kill a worker during a deployment | The worker was paused after the STAGE checkpoint, while holding the production deployment file lock. Its job and deployment were recorded as RUNNING. A competing lock acquisition was refused. The test sent SIGKILL and confirmed that process exited from that signal. |
| Start a fresh worker | After the recorded lease expired on the real clock, a different process completed the same job and deployment. Attempts increased from 1 to 2, the ownership fence increased, and execution resumed with activation and commit rather than repeating validation and staging. The HTTP probe observed the expected release digest. |
| Lose the original controller | A second, independent scenario created a backup through the CLI after a successful queued deployment. The archive stayed outside the controller directory. The original directory was then removed completely. |
| Restore in an empty directory | A fresh CLI process restored the archive without running setup first. Subsequent fresh processes read the restored edge, DNS record, deployment/job history and original release identity. API credential bytes and generated certificate/private-key bytes matched their originals; the restored private key had mode 0600. A non-default retention setting also survived. |
| Operate the rebuilt controller | The CLI changed cache policy. A fresh application process deployed that change, and the loopback endpoint reported the new release. Another process explicitly rolled back to the retained deployment. Both canonical input identity and the observed release returned to the original values. |

The worker interruption and controller-loss scenarios are separate tests with
independent temporary controllers. They do not rely on execution order or share
state with each other.

## What was real and what was simulated

**Real:** separate Python processes, SIGKILL, SQLite persistence, the production
cross-process deployment lock, startup contributions, durable job handlers,
wall-clock lease expiry, archive creation/inspection/restoration through the CLI,
filesystem deletion, release compilation and a real HTTP serving probe.

**Simulated:** Ansible and edge activation. A narrow test runner writes the
selected release digest into a temporary file. A loopback HTTP server reads it
at `/.blitzecdn/release`; it is not Nginx and executes no SSH or Docker commands.
The edge file lives outside the controller directory and survives its deletion.

The instrumented worker runs application startup and one production queue item;
it does not run the normal infinite worker entry-point loop. Its lease is reduced
to **2 seconds** for this practice. The production queue default remains
**900 seconds (15 minutes)**. No injected clock or database timestamp edit is
used to force expiry.

CLI commands perform setup, authoring, backup, restore and inspection. Deployment
submission, simulated execution and rollback use a private test-process helper
so real Ansible cannot run. All processes use this checkout's virtual environment;
this is not a separate installed-wheel recovery demonstration.

The archive contains the populated database, TLS and configuration components.
Certificate bytes are generated locally for the test. No ACME account is created,
no CA is contacted, and restored credentials are compared as bytes rather than
used to authenticate to a restarted API server. Actual SSH access, HTTPS service,
Compose restart and Ubuntu recovery remain unverified and deferred.

This covers interruption at one saved pre-activation checkpoint. It does not
establish recovery during actual Nginx activation or during a host failure.

## Repeat the practice

From a development checkout with dependencies installed:

```bash
just test-one tests/journeys/test_process_recovery.py -s
```

The test uses pytest-managed temporary directories, which pytest may retain for
debugging. It deliberately removes the original controller in the restore
scenario. It starts only a loopback listener and child processes it owns, and
kills only the child worker it created.
Run the test rather than invoking its private process helper against other state.

The practice requires a POSIX host and the source-checkout restore path. It is
explicitly skipped on Windows and inside Docker, where restore requires the host
Compose boundary. A skip is not a successful demonstration.

Evidence:

- [Worker interruption test](../tests/journeys/test_process_recovery.py):
  `test_killed_worker_resumes_its_saved_checkpoint`.
- [Controller loss and rollback test](../tests/journeys/test_process_recovery.py):
  `test_lost_controller_restores_and_rolls_back`.
- [Private process helper](../tests/recovery_process.py): only the Ansible runner
  is replaced; services, queue handling, lock and HTTP probe remain production code.

Final validation: `just test` passed **1,766 tests with 91.20% coverage**;
formatting, lint and strict types also passed. No public snapshots changed.

The focused run passed **2 tests in 55.62 seconds**. That duration is a local
observation, not a recovery-time guarantee. Its assertion-backed output was:

```text
RECOVERY: SIGKILL worker -> fresh worker; same job/deployment, attempt 2, checkpoint resumed
RECOVERY: original controller removed -> CLI restore in empty directory; state, credentials and PEM bytes match
RECOVERY: rebuilt controller deployed a change -> explicit rollback; canonical and observed release restored
```

## Findings for the next confirmed step

1. **The README's first site example uses retired syntax.** Running
   `domain add example.com --origin origin.example.com` exits 2 with
   `No such option: --origin`. Running `record add example.com cdn` without
   an address exits 2 with `Missing option '--value'`. Both were reproduced
   in a separate disposable directory. This practice instead registered the
   zone first and supplied the origin address through `record add --value`.
   Correct the operator examples in step 3, not the working CLI to fit stale prose.
2. **Restart does not mean immediate job takeover.** Reclaim waits for the old
   job lease to expire. Explain the production default and the `leased_until`
   field visible through job inspection so an operator can distinguish waiting
   from lost work. Do not shorten production leases merely to match this test.

No application defect was reproduced by these scenarios. Step 3 remains pending
confirmation; this step records the findings without changing operator behavior
or rewriting the quick-start instructions.
