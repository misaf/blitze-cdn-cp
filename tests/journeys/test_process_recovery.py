"""Kill a real disposable worker and rebuild a controller through fresh processes.

Ansible, Nginx and the CA are not exercised. A file-backed loopback listener
models the edge and survives deletion of the original controller directory.
"""

import http.server
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from paths import REPO_ROOT

from blitzecdn.core.ansible.lock import DeploymentLock
from blitzecdn.core.exceptions import DeploymentBusyError

pytestmark = pytest.mark.skipif(
    os.name != "posix" or Path("/.dockerenv").exists(),
    reason="requires POSIX process killing and host-side checkout restore",
)


@pytest.fixture
def practice(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "blitzecdn.toml").write_text("[blitzecdn]\nhistory_retention = 73\n")
    served = tmp_path / "edge-release"
    served.write_text("untracked\n")

    class Edge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/.blitzecdn/release"
            body = served.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Edge)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    port = server.server_address[1]

    def environment(project):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BLITZE_") and key != "COMPOSE_RESTORE_OFFLINE"
        }
        env["BLITZE_PROJECT_DIR"] = str(project)
        env["PYTHONPATH"] = str(REPO_ROOT / "tests")
        return env

    def run(project, arguments):
        result = subprocess.run(  # noqa: S603 - fixed interpreter and local test commands
            [sys.executable, *arguments],
            cwd=project,
            env=environment(project),
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def cli(project, *arguments):
        return run(
            project,
            ["-c", "from blitzecdn.entrypoints.cli.main import run; run()", *arguments],
        )

    def child_arguments(mode, *extra):
        return [
            str(REPO_ROOT / "tests/recovery_process.py"),
            mode,
            str(port),
            str(served),
            *extra,
        ]

    def child(project, mode, *extra):
        return json.loads(run(project, child_arguments(mode, *extra)))

    def start_worker(project):
        return subprocess.Popen(  # noqa: S603 - owned disposable test worker
            [sys.executable, *child_arguments("crash")],
            cwd=project,
            env=environment(project),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    try:
        cli(original, "setup")
        cli(
            original,
            "edge",
            "add",
            "edge-a",
            "--host",
            "127.0.0.1",
            "--public-address",
            "127.0.0.1",
            "--ssh-source",
            "127.0.0.1/32",
        )
        cli(original, "domain", "add", "example.com")
        cli(original, "domain", "ssl", "example.com", "--mode", "off")
        cli(original, "record", "add", "example.com", "cdn", "--value", "198.51.100.10")
        yield SimpleNamespace(
            original=original,
            served=served,
            cli=cli,
            child=child,
            start_worker=start_worker,
        )
    finally:
        server.shutdown()
        server.server_close()
        listener.join(timeout=5)


def test_killed_worker_resumes_its_saved_checkpoint(practice):
    original, served, child = practice.original, practice.served, practice.child
    submitted = child(original, "submit")
    deployment_id = submitted["deployments"][0]["id"]
    job_id = submitted["jobs"][0]["id"]
    assert submitted["deployments"][0]["status"] == "queued"

    worker = practice.start_worker(original)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(worker.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=45), (
                "worker never reached the crash checkpoint"
            )
        line = worker.stdout.readline()
        assert line, worker.stderr.read()
        paused = json.loads(line)
        assert paused == {"paused": "before-activate", "pid": worker.pid}
        interrupted = child(original, "snapshot")
        assert interrupted["deployments"][0]["status"] == "running"
        assert interrupted["deployments"][0]["targets"][0]["phase"] == "stage"
        job = interrupted["jobs"][0]
        assert job["status"] == "running" and job["attempts"] == 1
        assert job["leased_by"].endswith(f":{worker.pid}")
        # This is the production lock, held across the paused rollout.
        with (
            pytest.raises(DeploymentBusyError),
            DeploymentLock(original / ".state/deployment.lock"),
        ):
            pass
        worker.kill()
        worker.communicate(timeout=10)
        assert worker.returncode == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=10)

    # Wait for the recorded real lease; never advance an injected clock.
    remaining = (
        datetime.fromisoformat(job["leased_until"]) - datetime.now(UTC)
    ).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.05)
    resumed = child(original, "work")
    assert resumed["pid"] != paused["pid"]
    assert resumed["phases"] == ["activate", "commit"]
    recovered_job = resumed["jobs"][0]
    assert recovered_job["id"] == job_id
    assert recovered_job["status"] == "succeeded"
    assert recovered_job["attempts"] == 2 and recovered_job["fence"] > job["fence"]
    assert len(resumed["deployments"]) == len(resumed["jobs"]) == 1
    assert resumed["deployments"][0]["id"] == deployment_id
    assert resumed["deployments"][0]["status"] == "succeeded"
    assert served.read_text().strip() == resumed["release_digest"]
    print(
        "RECOVERY: SIGKILL worker -> fresh worker; same job/deployment, "
        "attempt 2, checkpoint resumed"
    )


def test_lost_controller_restores_and_rolls_back(practice, tmp_path, certificate_pair):
    original, served = practice.original, practice.served
    child, cli = practice.child, practice.cli
    child(original, "submit")
    resumed = child(original, "work")
    assert resumed["deployments"][0]["status"] == "succeeded"
    deployment_id = resumed["deployments"][0]["id"]
    good_release = resumed["release_id"]
    # Include real disposable PEM bytes, without contacting a CA or using TLS.
    certificate, private_key = certificate_pair()
    material = original / ".state/certificates/practice"
    material.mkdir(parents=True)
    (material / "fullchain.pem").write_bytes(certificate)
    (material / "privkey.pem").write_bytes(private_key)
    credentials = (original / ".env").read_bytes()
    archive = tmp_path / "saved-controller.tar.gz"
    cli(original, "backup", "create", "--output", str(archive))
    described = cli(original, "backup", "inspect", str(archive))
    assert "database" in described and "tls" in described and "config" in described
    shutil.rmtree(original)
    assert not original.exists()

    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    restored_output = cli(rebuilt, "backup", "restore", str(archive), "--yes")
    assert "blitzecdn deploy" in restored_output
    assert (rebuilt / ".env").read_bytes() == credentials
    assert (
        rebuilt / ".state/certificates/practice/privkey.pem"
    ).read_bytes() == private_key
    assert (
        rebuilt / ".state/certificates/practice/fullchain.pem"
    ).read_bytes() == certificate
    assert (
        rebuilt / ".state/certificates/practice/privkey.pem"
    ).stat().st_mode & 0o777 == 0o600
    assert json.loads(cli(rebuilt, "edge", "list", "--json"))[0]["name"] == "edge-a"
    assert (
        json.loads(cli(rebuilt, "record", "list", "example.com", "--json"))[0]["name"]
        == "cdn"
    )
    restored = child(rebuilt, "snapshot")
    assert restored["inputs_digest"] == resumed["inputs_digest"]
    assert restored["release_id"] == good_release
    assert restored["deployments"] == resumed["deployments"]
    assert restored["jobs"] == resumed["jobs"]
    assert restored["history_retention"] == 73
    print(
        "RECOVERY: original controller removed -> CLI restore in empty directory; "
        "state, credentials and PEM bytes match"
    )

    cli(rebuilt, "domain", "cache", "example.com", "--success", "1h")
    changed = child(rebuilt, "deploy")
    assert changed["release_id"] != good_release
    assert changed["deployments"][0]["status"] == "succeeded"
    assert served.read_text().strip() == changed["release_digest"]
    rollback = child(rebuilt, "rollback", "--deployment", deployment_id)
    assert rollback["deployments"][0]["status"] == "succeeded"
    assert rollback["release_id"] == good_release
    assert rollback["inputs_digest"] == resumed["inputs_digest"]
    assert served.read_text().strip() == resumed["release_digest"]
    print(
        "RECOVERY: rebuilt controller deployed a change -> explicit rollback; "
        "canonical and observed release restored"
    )
