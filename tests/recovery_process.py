"""One disposable process for the local recovery journey, never a real edge.

Production services, SQLite, file locking and serving verification run normally.
Only Ansible is replaced: activation writes a digest to a temporary file served
by the parent test. The crash mode blocks at an explicit checkpoint so the
parent can kill this process without running graceful shutdown.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from control_plane_fixtures import FakeRunner, build_application

from blitzecdn.capabilities.deployments.adapters.serving import ServingProbe
from blitzecdn.capabilities.http.policy import HttpScheme
from blitzecdn.composition import ApplicationServices, build_worker
from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import AnsibleRun


class LoopbackProbe(ServingProbe):
    def __init__(self, port: int) -> None:
        super().__init__(timeout=5)
        self.port = port

    def _connection(
        self, scheme: HttpScheme, address: str, port: int, hostname: str
    ) -> http.client.HTTPConnection:
        return super()._connection(scheme, address, self.port, hostname)


class LocalRunner:
    """A simulated edge runner using the production cross-process lock."""

    def __init__(self, settings: Settings, served: Path, *, pause: bool) -> None:
        self.settings = settings
        self.served = served
        self.pause = pause
        self.delegate = FakeRunner()
        self.phases: list[str] = []

    def lock(self) -> DeploymentLock:
        return DeploymentLock(self.settings.deployment_lock_path)

    def validate(self, variables: Path) -> AnsibleRun:
        return self.delegate.validate(variables)

    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun:
        phase = tags[0] if tags else "validate"
        self.phases.append(phase)
        if tags == ("activate",):
            if self.pause:
                print(
                    json.dumps({"paused": "before-activate", "pid": os.getpid()}),
                    flush=True,
                )
                # The test kills this process; EOF must not silently activate.
                sys.stdin.read()
                raise RuntimeError("crash checkpoint was released without a kill")
            document = yaml.safe_load(self.settings.generated_vars_path.read_text())
            self.served.write_text(document["blitzecdn_release_digest"] + "\n")
        return self.delegate.run(check=check, host_limit=host_limit, tags=tags)

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        return self.delegate.run_playbook(
            name=name, playbook=playbook, variables=variables, host_limit=host_limit
        )

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        return self.delegate.run_decommission(host_limit=host_limit)


def snapshot(control: ApplicationServices, runner: LocalRunner) -> dict[str, Any]:
    release = control.releases.compile()
    return {
        "pid": os.getpid(),
        "phases": runner.phases,
        "inputs_digest": control.releases.inputs().digest,
        "release_id": release.id,
        "release_digest": release.digest,
        "history_retention": control.settings.history_retention,
        "deployments": [
            {
                "id": item.id,
                "status": item.status.value,
                "release_id": item.release_id,
                "targets": [
                    {
                        "edge": target.edge,
                        "phase": target.phase,
                        "status": target.status,
                    }
                    for target in control.deployments.targets(item.id)
                ],
            }
            for item in control.deployments.list_deployments()
        ],
        "jobs": [
            {
                "id": job.id,
                "status": job.status.value,
                "attempts": job.attempts,
                "fence": job.fence,
                "leased_until": job.leased_until.isoformat()
                if job.leased_until
                else None,
                "leased_by": job.leased_by,
            }
            for job in control.job_queue.list_jobs()
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("submit", "crash", "work", "snapshot", "deploy", "rollback")
    )
    parser.add_argument("port", type=int)
    parser.add_argument("served", type=Path)
    parser.add_argument("--deployment")
    args = parser.parse_args()
    settings = Settings.from_environment()
    runner = LocalRunner(settings, args.served, pause=args.mode == "crash")
    worker = args.mode in {"crash", "work"}
    control = build_application(
        settings=settings,
        runner=runner,
        serving_probe=LoopbackProbe(args.port),
    )
    process = build_worker(control) if worker else None
    try:
        if process is not None:
            process.start()
            # Actual wall-clock expiry; production's default is 900 seconds.
            control.job_queue.run_one(lease_seconds=2)
        elif args.mode == "submit":
            control.deployments.submit_deployment("recovery-practice")
        elif args.mode == "deploy":
            control.deployments.deploy("recovery-practice")
        elif args.mode == "rollback":
            control.deployments.rollback("recovery-practice", args.deployment)
        print(json.dumps(snapshot(control, runner)), flush=True)
    finally:
        try:
            if process is not None:
                process.stop()
        finally:
            control.close()


if __name__ == "__main__":
    main()
