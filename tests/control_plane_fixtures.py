from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import nullcontext, suppress
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

import dramatiq
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from dramatiq.brokers.stub import StubBroker
from fastapi import FastAPI
from pydantic import SecretStr

from blitzecdn.api import create_app
from blitzecdn.capabilities.deployments.ports import QueueBackgroundRunner
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    DomainPatch,
    RecordType,
    Rule,
)
from blitzecdn.capabilities.dns.domain.hosts import host_name as host_name_for
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.cli import common as cli_common
from blitzecdn.composition import (
    ControlPlane,
    FleetRunner,
    Repository,
    load_control_plane_plugins,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import (
    AnsibleRun,
    HostRun,
    RunStatus,
    TaskOutcome,
    TaskResult,
)
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.worker import run_deployment, run_scheduled_job


@pytest.fixture(autouse=True)
def dramatiq_stub_broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubBroker]:
    """Keep unit and API tests independent of an external Redis process."""
    broker = StubBroker()
    monkeypatch.setattr(
        "blitzecdn.composition.control_plane.redis_ready", lambda _url: True
    )
    previous_broker = dramatiq.get_broker()
    actors: tuple[dramatiq.Actor[Any, Any], ...] = (run_deployment, run_scheduled_job)
    previous_actor_brokers = [actor.broker for actor in actors]
    dramatiq.set_broker(broker)
    for actor in actors:
        actor.broker = broker
        broker.declare_actor(actor)
    try:
        yield broker
    finally:
        dramatiq.set_broker(previous_broker)
        for actor, previous in zip(actors, previous_actor_brokers, strict=True):
            actor.broker = previous


@pytest.fixture(autouse=True)
def skip_tests_a_detached_capability_cannot_answer(
    request: pytest.FixtureRequest,
) -> None:
    """Run a root test that reads a capability's own output only while it is attached.

    The rendered edge configuration is composed: core frames the server block
    and each installed capability contributes the fragments that fill it. A
    test asserting on one of those fragments is a cross-package contract, so it
    belongs here rather than in either distribution — no package's own test
    environment has the other packages installed — but it cannot hold in the
    core-only workspace, where the fragment is not there to render.

    Named per test rather than per module, because most of the tests beside
    them assert core's half and must keep failing when it breaks.
    """
    required = getattr(request.module, "REQUIRES_CAPABILITIES", {})
    detached = sorted(
        capability
        for capability in required.get(request.function.__name__, ())
        if find_spec(f"blitzecdn_{capability}") is None
    )
    if detached:
        pytest.skip(f"detached: {', '.join(detached)}")


#: What a rule overrides when the caller asked for a distinct site and named no
#: policy — several sites in one zone, each holding its own certificate, which
#: is what the certificate and backup suites need.
#:
#: It has to override *something*: a rule that overrides nothing is refused,
#: because first match wins and such a rule would shadow every rule after it
#: while changing nothing. `enabled` restates the zone's own default, so the
#: rule is real without moving any desired state. That is enough to get a
#: separate host, because `derive_hosts` groups by which rule won rather than
#: by whether the resolved policies differ.
_IDENTITY_ONLY = {"enabled": True}


def seed_site(
    control: ControlPlane,
    *,
    name: str = "example-com",
    origin: str = "198.51.100.10",
    domain: str = "example.com",
    record: str = "cdn",
    record_type: RecordType = RecordType.A,
    ttl: int = 300,
    routed: bool = True,
    operator: str = "alice",
    **policy: object,
) -> CdnSite:
    """Set a zone's policy and proxy a hostname in it, then return the host.

    ``name`` decides where the policy goes rather than what anything is
    called: ``example-com`` is the zone's own policy, and anything else makes a
    rule claiming this hostname — which derives ``example-com--<record>``,
    whatever the caller asked for. Callers that need the name read it off the
    returned host.

    ``origin`` is where the proxied record points the edge: it is the record's
    ``value``, not a zone setting. ``routed=False`` sets the policy and proxies
    nothing, which derives no host at all — a zone we answer DNS for and serve
    nothing of.

    ``policy`` is any ``SitePolicy`` field, and it is set in both places: on
    the zone, and as the rule's overrides when ``name`` asked for one. A caller
    that names a rule and passes no policy gets :data:`_IDENTITY_ONLY`, which
    is what "a second site here, however it differs" has to be spelled as.

    The zone is created on first use so that several calls can share one domain
    without the caller tracking which was first.
    """
    with suppress(ConflictError):
        control.dns.create_domain(Domain(name=domain), operator)
    expected = host_name_for(domain, None)
    if name != expected:
        # A second policy in one zone is a rule, named after the hostname it
        # claims, so the host it derives is `<zone>--<label>`. A wildcard
        # record has no name a rule could take, so it gets one.
        rule = "wildcard" if record in {"*", "@"} else record.replace(".", "-")
        with suppress(ConflictError):
            control.rules.create_rule(
                Rule(
                    domain=domain,
                    name=rule,
                    match=f"{record}.{domain}",
                    overrides=dict(policy) or _IDENTITY_ONLY,
                ),
                operator,
            )
    else:
        rule = None
    if policy:
        control.dns.update_domain(
            domain,
            DomainPatch.model_validate(policy),
            operator,
        )
    if routed:
        seed_record(
            control,
            domain=domain,
            name=record,
            value=origin,
            record_type=record_type,
            ttl=ttl,
            operator=operator,
        )
        return control.sites.get_site(host_name_for(domain, rule))
    return CdnSite.model_validate(
        {
            **control.dns.get_domain(domain).model_dump(),
            "name": host_name_for(domain, rule),
            "server_names": (),
            "origin_host": origin,
        }
    )


_DEFAULT_ORIGINS = {RecordType.A: "198.51.100.10", RecordType.AAAA: "2001:db8::10"}


def seed_record(
    control: ControlPlane,
    *,
    domain: str = "example.com",
    name: str = "cdn",
    value: str | None = None,
    proxied: bool | None = None,
    record_type: RecordType = RecordType.A,
    ttl: int = 300,
    operator: str = "alice",
) -> DnsRecord:
    """Add one record, proxied or answering with ``value``.

    ``value`` is the record's address — the origin while proxied, the DNS
    answer when not — and defaults to a family-appropriate address. Use
    :func:`seed_site` for a zone with settings on it.
    """
    with suppress(ConflictError):
        control.dns.create_domain(Domain(name=domain), operator)
    return control.dns.create_record(
        DnsRecord.model_validate(
            {
                "domain": domain,
                "name": name,
                "type": record_type,
                "ttl": ttl,
                "value": value if value is not None else _DEFAULT_ORIGINS[record_type],
                "proxied": True if proxied is None else proxied,
            }
        ),
        operator,
    )


def host_run(
    name: str,
    *,
    ok: int = 4,
    changed: int = 0,
    failed: int = 0,
    unreachable: int = 0,
    report: dict[str, object] | None = None,
    changes: tuple[str, ...] = (),
    failure: str | None = None,
) -> HostRun:
    """One host's part in a fake run.

    `changes` and `failure` build the task lists the runner adapter produces,
    so a test can assert on what a report *says happened* rather than only on a
    count.
    """
    return HostRun(
        host=name,
        ok=ok,
        changed=changed or len(changes),
        failed=failed or (1 if failure else 0),
        unreachable=unreachable,
        changes=tuple(
            TaskResult(task=task, outcome=TaskOutcome.CHANGED) for task in changes
        ),
        failures=(
            (TaskResult(task="a task", outcome=TaskOutcome.FAILED, message=failure),)
            if failure
            else ()
        ),
        report=report,
    )


def ansible_run(
    *hosts: HostRun,
    status: RunStatus = RunStatus.SUCCEEDED,
    return_code: int | None = 0,
    log_path: str | None = "/var/log/blitzecdn/run.log",
    error: str | None = None,
    targeted: tuple[str, ...] = (),
) -> AnsibleRun:
    """What the runner hands back: per-host structured results plus a status.

    Tests build these directly rather than faking Ansible output, because the
    structured result *is* the contract now — there is no text for a double to
    imitate.
    """
    now = datetime.now(UTC)
    return AnsibleRun(
        id="run",
        playbook="edge.yml",
        status=status,
        return_code=return_code,
        started_at=now,
        finished_at=now,
        hosts=hosts,
        targeted=targeted,
        log_path=log_path,
        error=error,
    )


def origin_report(
    host: str,
    *,
    site: str = "example-com",
    origin: str = "origin.example.com:443",
    reachable: bool = True,
    tls_verified: object = True,
    detail: str = "",
) -> HostRun:
    """One edge's published origin report, as the runner adapter delivers it.

    Built as the role would render it — strings for the booleans, `-1` for a
    status that never arrived — because that crossing is exactly what the
    reader under test has to undo.
    """
    return host_run(
        host,
        report={
            "host": host,
            "collected_at": "2026-01-01T00:00:00Z",
            "origins": [
                {
                    "site": site,
                    "origin": origin,
                    "scheme": "https",
                    "ssl_mode": "full_strict",
                    "sni": "origin.example.com",
                    "reachable": str(reachable),
                    "tls_verified": str(tls_verified),
                    "status": "200" if reachable else "-1",
                    "detail": detail,
                }
            ],
        },
    )


class FakeRunner:
    def __init__(self, results: list[AnsibleRun] | None = None) -> None:
        self.results = results or [ansible_run(host_run("edge-a"))]
        self.check_modes: list[bool] = []
        self.host_limits: list[str | None] = []
        #: The scratch path each `validate` was handed, so a test can assert it
        #: was not the shared desired-state file.
        self.validated: list[Path] = []
        #: Every `run_playbook` call, as `(name, playbook, variables, limit)`.
        #: Generic because the runner is: purging a cache and collecting
        #: statistics are an installed capability's business now, and this
        #: double records what it was *asked to run* rather than growing a
        #: method per play some package might contribute.
        self.playbooks: list[tuple[str, Path, dict[str, object], str | None]] = []
        self.decommissions: list[str] = []

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self, variables: Path) -> AnsibleRun:
        self.validated.append(variables)
        return self.results[0]

    def run(self, *, check: bool, host_limit: str | None = None) -> AnsibleRun:
        self.check_modes.append(check)
        self.host_limits.append(host_limit)
        return self.results.pop(0)

    def run_playbook(
        self,
        *,
        name: str,
        playbook: Path,
        variables: Mapping[str, object],
        host_limit: str | None = None,
    ) -> AnsibleRun:
        self.playbooks.append((name, playbook, dict(variables), host_limit))
        return self.results.pop(0)

    def run_decommission(self, *, host_limit: str) -> AnsibleRun:
        self.decommissions.append(host_limit)
        return self.results.pop(0)


class FakeEdgeStore:
    """An in-memory ``EdgeStore``, for anything that only needs the roster.

    The real store is a SQLite table and also the thing the Ansible inventory
    plugin reads, so tests about the *plugin* use a real database (see
    `test_inventory.py`). Everything else — preflight comparing addresses, the
    runner expanding a host limit — only wants "which edges exist", and a list
    answers that without a file on disk.
    """

    def __init__(self, edges: list[Edge] | None = None) -> None:
        self.edges = list(edges if edges is not None else [edge("edge1")])

    def list_edges(self) -> list[Edge]:
        return list(self.edges)

    def get_edge(self, name: str) -> Edge:
        for candidate in self.edges:
            if candidate.name == name:
                return candidate
        raise NotFoundError(f"edge {name!r} does not exist")

    def create_edge(self, new: Edge) -> Edge:
        if any(candidate.name == new.name for candidate in self.edges):
            raise ConflictError(f"edge {new.name!r} already exists")
        self.edges.append(new)
        return new

    def replace_edge(self, edge: Edge, *, expected: Edge | None = None) -> Edge:
        """Replace one edge, refusing the write if it moved underneath.

        `expected` is the caller's copy of the row it read. The real store
        compares it against what is on disk and refuses a write that would
        overwrite someone else's, in that order — a name that does not exist
        is missing rather than conflicted, whatever `expected` says. The
        transaction the real one requires alongside `expected` is a fact about
        the database and has nothing to model here.
        """
        for index, candidate in enumerate(self.edges):
            if candidate.name == edge.name:
                if expected is not None and candidate != expected:
                    raise ConflictError(
                        f"edge {edge.name!r} changed while it was being edited"
                    )
                self.edges[index] = edge
                return edge
        raise NotFoundError(f"edge {edge.name!r} does not exist")

    def delete_edge(self, name: str) -> None:
        remaining = [candidate for candidate in self.edges if candidate.name != name]
        if len(remaining) == len(self.edges):
            raise NotFoundError(f"edge {name!r} does not exist")
        self.edges = remaining


def edge(name: str = "edge1", **overrides: object) -> Edge:
    """An edge with plausible defaults, for a test that does not care."""
    return Edge.model_validate(
        {"name": name, "host": f"{name}.example.net", **overrides}
    )


class RecordingBackgroundQueue:
    """Records durable deployment identifiers without requiring Redis."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def enqueue(self, deployment_id: str) -> None:
        self.ids.append(deployment_id)


class RefusingBackgroundQueue(RecordingBackgroundQueue):
    """A durable queue adapter that can fail publication."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = True

    def enqueue(self, deployment_id: str) -> None:
        if self.refuse:
            raise RuntimeError("can't publish to queue")
        super().enqueue(deployment_id)


if TYPE_CHECKING:
    # The doubles above stand in for ports, and this is where that claim is
    # *checked* rather than asserted in a docstring. `FleetRunner`'s own
    # docstring already names "a test that injects a fake runner" as its other
    # implementer; before this block nothing held that to be true, and every
    # call site had grown a `# type: ignore[arg-type]` which suppressed nothing
    # because the suite is outside mypy's scope. A double that drifts from its
    # port now fails `just types`, in this file, where the double is.
    _runner_is_a_fleet_runner: FleetRunner = FakeRunner()
    _edge_store_is_an_edge_store: EdgeStorePort = FakeEdgeStore()
    _queue_is_a_background_runner: QueueBackgroundRunner = RecordingBackgroundQueue()
    _refusing_queue_is_one_too: QueueBackgroundRunner = RefusingBackgroundQueue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ansible = tmp_path / "ansible"
    (ansible / "inventory").mkdir(parents=True)
    (ansible / "playbooks").mkdir()
    (ansible / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    inventory = ansible / "inventory/hosts.yml"
    inventory.write_text(
        "all:\n  children:\n    blitzecdn_edges:\n      hosts: {}\n",
        encoding="utf-8",
    )
    playbook = ansible / "playbooks/edge.yml"
    playbook.write_text("- hosts: blitzecdn_edges\n  tasks: []\n", encoding="utf-8")
    state = tmp_path / "state"
    return Settings(
        project_dir=tmp_path,
        state_dir=state,
        database_path=state / "control-plane.db",
        ansible_dir=ansible,
        inventory_path=inventory,
        playbook_path=playbook,
        generated_vars_path=state / "desired-state.yml",
        deployment_lock_path=state / "deployment.lock",
        certificate_dir=state / "certificates",
        environment_path=tmp_path / ".env",
        decommission_playbook_path=ansible / "playbooks/decommission.yml",
        ansible_playbook="/usr/bin/true",
        api_keys={"tester": SecretStr("x" * 32)},
    )


def with_capability_settings(settings: Settings, **values: object) -> Settings:
    """A copy of ``settings`` carrying capability configuration.

    An optional capability's settings are not fields on ``Settings`` any more,
    so a test configures one the way an operator does — by the `BLITZE_*` name
    its package claims — rather than by `model_copy(update=...)` against a
    field core no longer has. Names are given unprefixed and lowercase, as they
    are written in ``blitzecdn.toml``.
    """
    return settings.model_copy(
        update={
            "capability_environment": {
                **settings.capability_environment,
                **{
                    "BLITZE_" + name.upper(): SecretStr(str(value))
                    for name, value in values.items()
                },
            }
        }
    )


@pytest.fixture
def site_payload() -> dict[str, object]:
    """A site with one hostname routed to it.

    ``server_names`` is here because this payload is also used to *build* a
    `CdnSite` in tests that never touch a database. Through the API and the
    services it is maintained by `dns` and cannot be set.
    """
    return {
        "name": "example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "198.51.100.10",
    }


@pytest.fixture
def domain_payload() -> dict[str, object]:
    return {"name": "example.com"}


@pytest.fixture
def record_payload() -> dict[str, object]:
    return {
        "domain": "example.com",
        "name": "cdn",
        "type": "A",
        "value": "198.51.100.10",
        "proxied": True,
    }


@pytest.fixture
def seeded(settings: Settings) -> Callable[..., tuple[ControlPlane, Repository]]:
    """A control plane holding one site with one hostname routed to it.

    Returns ``(control, repository)``.
    """

    def build(runner: FleetRunner | None = None) -> tuple[ControlPlane, Repository]:

        repository = Repository(settings.database_path)
        control = ControlPlane(
            settings=settings,
            repository=repository,
            runner=runner or FakeRunner(),
        )
        seed_site(control, operator="tester")
        return control, repository

    return build


#: RSA-2048 generation costs about a tenth of a second, and the suite asks for
#: well over a hundred certificates. Nothing asserts on the modulus, only on
#: whether a key *matches* the certificate beside it, so a small pool of keys
#: reused across certificates proves exactly what generating each one afresh
#: did. The pool is built once per process — which under xdist means once per
#: worker — and the keys are immutable, so it carries no state between tests.
_KEY_POOL_SIZE = 4
_key_pool: list[rsa.RSAPrivateKey] = []


def _pooled_key(index: int) -> rsa.RSAPrivateKey:
    while len(_key_pool) <= index:
        _key_pool.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _key_pool[index]


@pytest.fixture(scope="session")
def rsa_keys() -> tuple[rsa.RSAPrivateKey, ...]:
    """Distinct cached RSA keys, for a test that needs two that disagree."""
    return tuple(_pooled_key(index) for index in range(_KEY_POOL_SIZE))


def private_key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture
def certificate_pair() -> Callable[..., tuple[bytes, bytes]]:
    #: Per test, not per session: successive calls within one test hand back
    #: *different* keys, so a test that installs one pair over another can still
    #: tell the two apart on disk.
    issued = 0

    def generate(
        domains: tuple[str, ...] = ("cdn.example.com",),
        *,
        valid: bool = True,
        days: int = 30,
    ) -> tuple[bytes, bytes]:
        """``days`` is the remaining lifetime, for exercising expiry logic."""
        nonlocal issued
        key = _pooled_key(issued % _KEY_POOL_SIZE)
        issued += 1
        now = datetime.now(UTC)
        start = now - timedelta(days=1) if valid else now - timedelta(days=10)
        end = now + timedelta(days=days) if valid else now - timedelta(days=1)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domains[0])])
            )
            .issuer_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
            )
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name) for name in domains]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM), private_key_pem(
            key
        )

    return generate


def cli_control_plane(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    runner_double: FleetRunner | None = None,
) -> ControlPlane:
    """A control plane wired to doubles, with the CLI pointed at it.

    Shared because more than one distribution's CLI tests need it: the command
    tree an operator sees is assembled from every installed plugin, so a test
    of `blitzecdn cache purge` drives the same root application as a test of
    `blitzecdn deploy` and needs the same substitution to do it.
    """
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=runner_double or FakeRunner(),
    )
    monkeypatch.setattr(cli_common, "control_plane", lambda: control)
    monkeypatch.setattr(cli_common, "settings", lambda: settings)
    return control


def repository_on(settings: Settings) -> Repository:
    """A second handle on the test database, for seeding and reading back.

    The control plane does not hand out its stores — that is the point of the
    layering rule — so a test that wants to plant a derived site or read the
    audit trail straight from SQLite opens its own handle on the same file
    rather than reaching through the object under test.
    """
    return Repository(settings.database_path)


def control_plane_app(settings: Settings) -> FastAPI:
    """The all-package workspace API used by cross-capability integration tests."""
    return create_app(settings, plugins=load_control_plane_plugins())


#: The API key `settings` configures, as the header a client sends. Shared
#: rather than spelled per module: every HTTP suite in the workspace needs it,
#: and an optional distribution's tests cannot reach a helper defined inside
#: `tests/api/test_api.py`.
API_HEADERS = {"X-API-Key": "x" * 32}


def seed_site_over_http(
    client: Any,
    headers: Mapping[str, str] = API_HEADERS,
    *,
    name: str = "example-com",
    label: str = "cdn",
    **policy: object,
) -> Any:
    """Zone, policy, record — over HTTP, the way a client would.

    Still three calls: the policy is set on the zone, and the record proxies
    the hostname with its value as the origin the edge fetches from.
    """
    client.post("/v1/domains", json={"name": "example.com"}, headers=headers)
    created = client.patch(
        "/v1/domains/example.com",
        json={**policy},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    proxied = client.post(
        "/v1/domains/example.com/records",
        json={
            "domain": "example.com",
            "name": label,
            "value": "198.51.100.10",
            "proxied": True,
        },
        headers=headers,
    )
    assert proxied.status_code == 201, proxied.text
    return created.json()
