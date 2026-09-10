"""Host-side Docker SDK runner, copied by Ansible into /etc/blitzecdn.

This file runs with the bootstrap interpreter without importing the application.
The managed Compose document remains the service definition; this adapter accepts
only the subset emitted by our role and rejects unsupported settings before it
stops anything. Compose still owns installation and image builds.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import docker
import yaml
from docker.errors import DockerException
from docker.models.containers import Container
from dotenv import dotenv_values

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
WRITERS = ("blitzecdn-api", "blitzecdn-worker")


class HostRuntimeError(Exception):
    """An operation could not safely complete."""


class ServiceDefinitions:
    """Translate our managed service definitions into Docker SDK arguments."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.document = yaml.safe_load(path.read_text())
        self.project: str = self.document["name"]
        self.services: dict[str, Any] = self.document["services"]
        # Validate every service before discovery, shutdown, or container creation.
        for name in self.services:
            self.options(name)

    def options(self, name: str) -> dict[str, Any]:
        service = self.services[name]
        supported = {
            "image",
            "build",
            "env_file",
            "environment",
            "network_mode",
            "volumes",
            "security_opt",
            "container_name",
            "command",
            "restart",
            "healthcheck",
            "profiles",
            "entrypoint",
        }
        unsupported = service.keys() - supported
        if unsupported:
            raise HostRuntimeError(
                f"Unsupported service settings for {name}: {sorted(unsupported)}"
            )
        environment = {}
        for filename in service.get("env_file", []):
            path = self.path.parent / filename
            if not path.is_file():
                raise HostRuntimeError(f"Missing environment file: {path}")
            environment.update(dotenv_values(path, interpolate=False))
        environment.update(service.get("environment", {}))
        volumes = {}
        for mount in service.get("volumes", []):
            source, target, *mode = mount.split(":")
            if not source.startswith("/"):
                source = self.document["volumes"][source]["name"]
            volumes[source] = {"bind": target, "mode": mode[0] if mode else "rw"}
        options: dict[str, Any] = {
            "image": service["image"],
            "environment": environment,
            "volumes": volumes,
            "restart_policy": {"Name": service.get("restart", "no")},
            "labels": {
                PROJECT_LABEL: self.project,
                SERVICE_LABEL: name,
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.container-number": "1",
                # Compose discovers managed containers by the presence of this
                # label. Its own hash differs, so the next install reconciles
                # the SDK-created container from the authoritative service file.
                "com.docker.compose.config-hash": "blitzecdn-sdk",
            },
        }
        for field in ("command", "entrypoint", "network_mode", "security_opt"):
            if field in service:
                options[field] = service[field]
        if "container_name" in service:
            options["name"] = service["container_name"]
        if "healthcheck" in service:
            options["healthcheck"] = self.healthcheck(service["healthcheck"])
        return options

    @staticmethod
    def healthcheck(settings: dict[str, Any]) -> dict[str, Any]:
        health = dict(settings)
        for field in ("interval", "timeout", "start_period"):
            if field in health:
                value = str(health[field])
                if not value.endswith("s") or not value[:-1].isdigit():
                    raise HostRuntimeError(f"Unsupported health duration: {value}")
                health[field] = int(value[:-1]) * 1_000_000_000
        return health


class HostRuntime:
    """Own disposable commands and offline restore recovery on the Docker host."""

    def __init__(
        self, client: docker.DockerClient, definitions: ServiceDefinitions
    ) -> None:
        self.client = client
        self.definitions = definitions

    def containers(self, service: str, *, all_states: bool = False) -> list[Container]:
        return self.client.containers.list(
            all=all_states,
            filters={
                "label": [
                    f"{PROJECT_LABEL}={self.definitions.project}",
                    f"{SERVICE_LABEL}={service}",
                    "com.docker.compose.oneoff=False",
                ],
            },
        )

    def wait_healthy(self, containers: list[Container], timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        pending = list(containers)
        while pending:
            for container in pending[:]:
                container.reload()
                state = container.attrs["State"]
                if (
                    state["Status"] == "running"
                    and state.get("Health", {}).get("Status") == "healthy"
                ):
                    pending.remove(container)
                elif (
                    state["Status"] in {"exited", "dead"}
                    or state.get("Health", {}).get("Status") == "unhealthy"
                ):
                    raise HostRuntimeError(f"Container {container.name} is not healthy")
            if pending:
                if time.monotonic() >= deadline:
                    raise HostRuntimeError(
                        "Timed out waiting for: "
                        + ", ".join(str(c.name) for c in pending)
                    )
                time.sleep(1)

    def recover(self, running: list[Container]) -> None:
        failures = []
        recovered = []
        for previous in running:
            service = previous.labels[SERVICE_LABEL]
            try:
                # Read the environment again, after restored credentials are published.
                options = self.definitions.options(service)
                previous.remove(force=True)
                replacement = self.client.containers.create(**options)
                replacement.start()
                recovered.append(replacement)
            except (DockerException, OSError, HostRuntimeError) as error:
                failures.append(f"{service}: {error}")
        try:
            self.wait_healthy(recovered)
        except (DockerException, HostRuntimeError) as error:
            failures.append(str(error))
        if failures:
            raise HostRuntimeError(
                "Control-plane recovery failed: " + "; ".join(failures)
            )

    def execute(self, arguments: list[str]) -> int:
        backup = arguments[:1] == ["backup"]
        restore = arguments[:2] == ["backup", "restore"]
        options = self.definitions.options("blitzecdn-cli")
        if "BLITZE_ALLOW_EMPTY_SITES" in os.environ:
            options["environment"]["BLITZE_ALLOW_EMPTY_SITES"] = os.environ[
                "BLITZE_ALLOW_EMPTY_SITES"
            ]
        with contextlib.ExitStack() as stack:
            stage = None
            if backup:
                stage = Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="blitzecdn-backup-")
                    )
                )
                self.stage_configuration(stage, options)
            if not restore:
                # Nothing to start first. A disposable CLI container used to
                # have to bring up the message broker its command would talk
                # to; durable work lives in the state volume now, which this
                # container already mounts.
                return self.run_command(arguments, options)
            # Complete discovery before stopping anything. A failed stop is still
            # inside the recovery boundary, so every prior writer is recovered.
            running = [
                container
                for service in WRITERS
                for container in self.containers(service)
            ]
            try:
                for container in running:
                    container.stop(timeout=30)
                options["environment"]["COMPOSE_RESTORE_OFFLINE"] = "1"
                status = self.run_command(arguments, options)
                if status == 0 and stage is not None:
                    self.publish_configuration(stage)
                return status
            finally:
                self.recover(running)

    def configuration_paths(self) -> dict[str, Path]:
        service = self.definitions.services["blitzecdn-cli"]
        config = next(
            mount.split(":")[0]
            for mount in service["volumes"]
            if mount.split(":")[1] == "/opt/blitzecdn/blitzecdn.toml"
        )
        return {
            "blitzecdn.toml": Path(config),
            "blitzecdn.env": self.definitions.path.parent / service["env_file"][0],
        }

    def stage_configuration(self, stage: Path, options: dict[str, Any]) -> None:
        for filename, source in self.configuration_paths().items():
            target = stage / filename
            shutil.copyfile(source, target)
            target.chmod(0o600)
            os.chown(target, 65534, 65534)
        (stage / ".state").mkdir()
        os.chown(stage / ".state", 65534, 65534)
        os.chown(stage, 65534, 65534)
        state = next(
            source
            for source, mount in options["volumes"].items()
            if mount["bind"] == "/opt/blitzecdn/.state"
        )
        # Docker accepts multiple mounts of the same source via Mount objects.
        options["mounts"] = [
            docker.types.Mount("/run/blitzecdn-backup/.state", state, type="bind")
        ]
        options["volumes"][str(stage)] = {"bind": "/run/blitzecdn-backup", "mode": "rw"}
        options["environment"].update(
            {
                "BLITZE_PROJECT_DIR": "/run/blitzecdn-backup",
                "BLITZE_CONFIG": "/run/blitzecdn-backup/blitzecdn.toml",
                "BLITZE_ENVIRONMENT_PATH": "/run/blitzecdn-backup/blitzecdn.env",
            }
        )

    def publish_configuration(self, stage: Path) -> None:
        for filename, target in self.configuration_paths().items():
            content = (stage / filename).read_bytes()
            if target.read_bytes() == content:
                continue
            descriptor, temporary = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.restore-"
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    os.fchmod(
                        output.fileno(),
                        0o644 if filename == "blitzecdn.toml" else 0o600,
                    )
                    os.fchown(output.fileno(), 0, 0)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)

    def run_command(self, arguments: list[str], options: dict[str, Any]) -> int:
        options["labels"]["com.docker.compose.oneoff"] = "True"
        container = self.client.containers.create(
            **{**options, "command": arguments, "stdin_open": True}
        )
        stopped = threading.Event()
        try:
            # Attach before start so even very short commands retain their output.
            output = container.attach(stream=True, logs=True, demux=True)
            connection: Any = container.attach_socket(params={"stdin": 1, "stream": 1})
            # Close the HTTP response before its raw SocketIO, so its finalizer
            # does not attempt to flush an already closed file on Python 3.14.
            with contextlib.closing(connection._response):
                # Docker's SDK exposes a read-only SocketIO around the Unix
                # socket. Send stdin through that socket, not SocketIO.write().
                sender = threading.Thread(
                    target=forward_input, args=(connection, stopped), daemon=True
                )
                container.start()
                sender.start()
                try:
                    for stdout, stderr in output:
                        if stdout:
                            sys.stdout.buffer.write(stdout)
                            sys.stdout.buffer.flush()
                        if stderr:
                            sys.stderr.buffer.write(stderr)
                            sys.stderr.buffer.flush()
                    return int(container.wait()["StatusCode"])
                finally:
                    stopped.set()
                    sender.join(timeout=1)
        finally:
            stopped.set()
            container.remove(force=True)


def forward_input(connection: Any, stopped: threading.Event) -> None:
    """Forward prompts and piped input without a shell or Docker subprocess."""
    try:
        while not stopped.is_set():
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                continue
            data = os.read(sys.stdin.fileno(), 65536)
            if not data:
                connection._sock.shutdown(socket.SHUT_WR)
                return
            connection._sock.sendall(data)
    except (OSError, ValueError):
        # The command can finish while stdin is still open.
        return


def interrupted(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", required=True, type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        definitions = ServiceDefinitions(args.services)
        # This runner publishes host files and must use this host's daemon.
        with contextlib.closing(
            docker.DockerClient(base_url="unix:///var/run/docker.sock", version="auto")
        ) as client:
            return HostRuntime(client, definitions).execute(
                args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            )
    except (DockerException, OSError, HostRuntimeError) as error:
        print(f"blitzecdn: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
