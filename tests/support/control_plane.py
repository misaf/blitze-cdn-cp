from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from dishka import Container, Provider, make_container
from dishka.integrations.fastapi import FastapiProvider
from fastapi import FastAPI
from pydantic import SecretStr

from blitzecdn.capabilities.cache.service import CacheService
from blitzecdn.capabilities.deployments.domain import Deployment, DeploymentStatus
from blitzecdn.capabilities.deployments.ports import QueueBackgroundRunner
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.dns import DnsService
from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    DomainPatch,
    PatchPolicy,
    RecordType,
    Rule,
    RulePatch,
)
from blitzecdn.capabilities.dns.domain.hosts import host_name as host_name_for
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.capabilities.dns.service import HostService, RuleService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.domain import Edge
from blitzecdn.capabilities.edges.ports import EdgeStore as EdgeStorePort
from blitzecdn.capabilities.jobs.service import (
    JobQueue,
    JobRunner,
    JobScheduler,
    OperationService,
)
from blitzecdn.capabilities.origins.service.checks import OriginCheckService
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.tls.certificates.adapters import CertificateStore
from blitzecdn.capabilities.tls.certificates.ports import SiteEditor
from blitzecdn.capabilities.tls.certificates.service import CertificateService
from blitzecdn.capabilities.tls.contract import CertificateMode
from blitzecdn.composition import (
    Adapters,
    ApiNode,
    ApiProvider,
    FleetRunner,
    InfrastructureProvider,
    Repository,
    ServiceProvider,
    Worker,
    WorkerProvider,
    load_registry,
)
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.config import Settings
from blitzecdn.core.domain.runs import (
    AnsibleRun,
    HostRun,
    RunStatus,
    TaskOutcome,
    TaskResult,
)
from blitzecdn.core.exceptions import ConflictError, NotFoundError
from blitzecdn.core.features import (
    FeatureRegistry,
    Operations,
    Rendering,
    ScheduledJob,
)
from blitzecdn.core.persistence.audit import AuditLog
from blitzecdn.core.persistence.schema import DatabaseSchema
from blitzecdn.core.ports.operations import AuditTrail
from blitzecdn.entrypoints.api import create_app
from blitzecdn.entrypoints.cli import common as cli_common
from support.policy import (
    TEST_CERTIFICATE_VERSION,
    domain_from_settings,
    site_from_settings,
)


@pytest.fixture(autouse=True)
def serving_probe_opens_no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test that deploys off the network.

    A deployment's last phase asks the edge over HTTP whether it is serving. This
    replaces the default probe, so a control plane built without one gets an
    answer instead of a socket. Tests pass their own `serving_probe=` to override
    it; `tests/capabilities/deployments/test_serving_probe.py` exercises the real
    one.
    """
    monkeypatch.setattr(
        "blitzecdn.composition.infrastructure.ServingProbe",
        lambda *args, **kwargs: FakeServingProbe(),
    )


#: What a rule overrides when the caller asked for a distinct site and named no
#: policy, for suites needing several sites in one zone.
#:
#: A rule that overrides nothing is refused, so `enabled` restates the zone's
#: default. That is enough for a separate host: `derive_hosts` groups by which
#: rule won.
_IDENTITY_ONLY = {"enabled": True}


def _issuer_owned_mode(policy: Mapping[str, object]) -> CertificateMode | None:
    """The controller-managed mode a caller asked for, if it asked for one."""
    mode = policy.get("certificate_mode")
    if mode is None:
        return None
    parsed = CertificateMode(str(mode))
    return parsed if parsed.issuer_owned else None


def _set_policy(
    control: ControlPlane,
    domain: str,
    rule: str | None,
    policy: Mapping[str, object],
    operator: str,
) -> None:
    """Write policy where this host's policy lives: its zone, or its rule.

    As ``HostService._apply_to_source`` does: a rule's settings must not merge
    into the zone, where every other hostname would pick them up.
    """
    if rule is None:
        control.dns.update_domain(
            domain, PatchPolicy.from_patch(DomainPatch.model_validate(policy)), operator
        )
        return
    control.rules.update_rule(
        domain,
        rule,
        RulePatch(
            overrides={**control.rules.get_rule(domain, rule).overrides, **policy}
        ),
        operator,
    )


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

    ``name`` decides where the policy goes: ``example-com`` is the zone's own, and
    anything else makes a rule claiming this hostname, deriving
    ``example-com--<record>``. Callers read the name off the returned host.

    ``origin`` is the proxied record's ``value``. ``routed=False`` sets the policy
    and proxies nothing, so no host is derived.

    ``policy`` takes ``SitePolicy`` fields, set on the zone and as the rule's
    overrides. A rule with no policy gets :data:`_IDENTITY_ONLY`. The zone is
    created on first use.

    A controller-managed ``certificate_mode`` is seeded the way the issuer reaches
    it: the operator's policy, then ``activate_managed_certificate``, then the TLS
    mode that needs a certificate.
    """
    policy = dict(policy)
    issuer_mode = _issuer_owned_mode(policy)
    if issuer_mode is not None:
        # Held back and replayed below in the order the system reaches them.
        deferred_ssl = policy.pop("ssl_mode", None)
        for field in ("certificate_mode", "certificate_path", "certificate_key_path"):
            policy.pop(field, None)
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
            PatchPolicy.from_patch(DomainPatch.model_validate(policy)),
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
        site = control.sites.get_site(host_name_for(domain, rule))
        if issuer_mode is None:
            return site
        # The issuer's own write, which derives the paths from this host's name.
        control.site_editor.activate_managed_certificate(
            site, issuer_mode, TEST_CERTIFICATE_VERSION
        )
        if deferred_ssl is not None:
            _set_policy(control, domain, rule, {"ssl_mode": deferred_ssl}, operator)
        return control.sites.get_site(host_name_for(domain, rule))
    return site_from_settings(
        {
            **control.dns.get_domain(domain).model_dump(),
            "name": host_name_for(domain, rule),
            "server_names": (),
            "origin_host": origin,
        }
    )


_DEFAULT_ORIGINS = {RecordType.A: "198.51.100.10", RecordType.AAAA: "2001:db8::10"}


def seed_edge(
    control: ControlPlane,
    *,
    name: str = "edge-a",
    host: str = "192.0.2.10",
    public_address: str | None = None,
    capabilities: tuple[str, ...] | None = None,
) -> Edge:
    """Register one edge, so a deployment has somewhere to go.

    Addresses default to TEST-NET-1; the serving probe is a double here.
    """
    return control.edges.add_edge(
        Edge(
            name=name,
            host=host,
            ssh_sources=("198.51.100.0/24",),
            public_addresses=(public_address,) if public_address else (),
            capabilities=capabilities,
        ),
        "tester",
    )


class FakeServingProbe:
    """A serving verifier that answers without a network.

    Records what it was asked and answers as told.
    """

    def __init__(self, *, served: bool = True) -> None:
        self.served = served
        #: Every call, as `(edge, address, hostnames)`.
        self.asked: list[tuple[str, str, tuple[str, ...]]] = []

    def verify(
        self, *, edge: str, address: str, sites: Sequence[CdnSite], release_digest: str
    ) -> tuple[Any, ...]:
        hostnames = tuple(site.server_names[0] for site in sites if site.server_names)
        self.asked.append((edge, address, hostnames))
        return tuple(
            SimpleNamespace(
                edge=edge,
                hostname=hostname,
                served=self.served,
                detail="HTTP 200" if self.served else "connection refused",
            )
            for hostname in hostnames
        )


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
        control.dns.create_domain(domain_from_settings({"name": domain}), operator)
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

    `changes` and `failure` build the task lists the runner adapter produces, so a
    test can assert on what a report says happened.
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
    """What the runner hands back: per-host structured results plus a status."""
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

    Rendered as the role would (strings for booleans, `-1` for a missing status),
    since undoing that is the reader's job.
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
        self.playbooks: list[tuple[str, Path, dict[str, object], str | None]] = []
        self.decommissions: list[str] = []
        #: The `--tags` selection each run was made with. `("stage",)` is the
        #: staging phase; empty is a full converge.
        self.tag_selections: list[tuple[str, ...]] = []

    def lock(self) -> nullcontext[None]:
        return nullcontext()

    def validate(self, variables: Path) -> AnsibleRun:
        self.validated.append(variables)
        return self.results[0]

    def run(
        self,
        *,
        check: bool,
        host_limit: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> AnsibleRun:
        self.check_modes.append(check)
        self.host_limits.append(host_limit)
        self.tag_selections.append(tags)
        # Repeats the last result once the list is spent: one result means every run
        # answers this, and a sequence is played in order.
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

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

    Tests of the inventory feature use a real database (`test_inventory.py`).
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

        `expected` is the caller's copy of the row. A missing name is missing rather
        than conflicted, whatever `expected` says.
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
    # The doubles above stand in for ports; `just types` checks that here.
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
    configured = Settings(
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
    DatabaseSchema.migrate(configured.database_path)
    return configured


def with_capability_settings(settings: Settings, **values: object) -> Settings:
    """A copy of ``settings`` carrying capability configuration.

    Configured by the `BLITZE_*` name the capability claims, as an operator does.
    Names are given unprefixed and lowercase, as in ``blitzecdn.toml``.
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

    ``server_names`` is here because the payload also builds a `CdnSite` directly;
    through the API and services `dns` maintains it.
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
def record_create_payload(record_payload: dict[str, object]) -> dict[str, object]:
    """HTTP record creation takes the domain from the URL."""
    return {name: value for name, value in record_payload.items() if name != "domain"}


@pytest.fixture
def seeded(settings: Settings) -> Callable[..., tuple[ControlPlane, Repository]]:
    """A control plane holding one site with one hostname routed to it.

    Returns ``(control, repository)``.
    """

    def build(
        runner: FleetRunner | None = None,
    ) -> tuple[ControlPlane, Repository]:
        repository = Repository(settings.database_path)
        control = build_application(
            settings=settings,
            repository=repository,
            runner=runner or FakeRunner(),
        )
        seed_site(control, operator="tester")
        return control, repository

    return build


#: RSA-2048 generation is slow and nothing asserts on the modulus, only on
#: whether a key matches its certificate. So a small pool of immutable keys is
#: built once per process (per xdist worker) and reused.
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
    #: Per test: successive calls in one test return different keys, so a test
    #: installing one pair over another can tell them apart.
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
    providers: Sequence[Provider] = (),
) -> ControlPlane:
    """A control plane wired to doubles, with the CLI pointed at it.

    The command tree is assembled from every feature, so every CLI test drives the
    same root application.
    """
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=runner_double or FakeRunner(),
        providers=providers,
    )
    monkeypatch.setattr(cli_common, "container", lambda: control.container)
    monkeypatch.setattr(cli_common, "settings", lambda: settings)
    return control


class _Resolved[T]:
    """A service read off the test's container by attribute."""

    def __init__(self, kind: type[T]) -> None:
        self.kind = kind

    def __get__(self, control: ControlPlane, owner: object = None) -> T:
        return control.container.get(self.kind)


class ControlPlane:
    """The production graph every process shares, read by attribute in tests.

    One container with the API's and the worker's additions, so a test can
    serve it over HTTP and run its queue. Resolving :attr:`worker` installs
    the queue's handlers, as starting a worker does.
    """

    def __init__(self, container: Container) -> None:
        self.container = container

    settings = _Resolved(Settings)
    repository = _Resolved(Repository)
    features = _Resolved(FeatureRegistry)
    edge_contributions = _Resolved(EdgeContributions)
    certificate_store = _Resolved(CertificateStore)
    job_queue = _Resolved(JobQueue)
    job_scheduler = _Resolved(JobScheduler)
    job_runner = _Resolved(JobRunner)
    dns = _Resolved(DnsService)
    hosts = _Resolved(HostService)
    rules = _Resolved(RuleService)
    rendering = _Resolved(Rendering[CdnSite])
    releases = _Resolved(ReleaseService)
    deployments = _Resolved(DeploymentService)
    edges = _Resolved(EdgeOperationsService)
    cache = _Resolved(CacheService)
    origins = _Resolved(OriginCheckService)
    queued_operations = _Resolved(OperationService)
    certificates = _Resolved(CertificateService)
    automatic_ssl = _Resolved(AutomaticSslService)
    operations = _Resolved(Operations)
    api = _Resolved(ApiNode)
    worker = _Resolved(Worker)

    # Ports are Protocols, which `type[T]` cannot name.

    @property
    def runner(self) -> FleetRunner:
        return cast(FleetRunner, self.container.get(FleetRunner))

    @property
    def sites(self) -> SiteReader:
        return cast(SiteReader, self.container.get(SiteReader))

    @property
    def site_editor(self) -> SiteEditor:
        return cast(SiteEditor, self.container.get(SiteEditor))

    @property
    def audit(self) -> AuditTrail:
        return cast(AuditTrail, self.container.get(AuditTrail))

    @property
    def transactions(self) -> Repository:
        return self.repository

    @property
    def events(self) -> AuditLog:
        return self.repository.audit_log

    @property
    def jobs(self) -> Mapping[str, ScheduledJob]:
        return self.operations.scheduled_jobs

    def close(self) -> None:
        self.container.close()


def build_application(
    *, settings: Settings, providers: Sequence[Provider] = (), **adapters: Any
) -> ControlPlane:
    """The production service graph over real adapters, except those passed.

    ``adapters`` are :class:`Adapters`' fields — ``repository``, ``runner``,
    ``serving_probe`` and the rest — so a test replaces only what it is about.
    ``providers`` decorate services the graph built.
    """
    return ControlPlane(
        make_container(
            InfrastructureProvider(),
            ServiceProvider(),
            ApiProvider(),
            WorkerProvider(),
            FastapiProvider(),
            *providers,
            context={Settings: settings, Adapters: Adapters(**adapters)},
        )
    )


class _Borrowed:
    """A test's container lent to the API, which closes what its lifespan built."""

    def __init__(self, container: Container) -> None:
        self._container = container

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._container(*args, **kwargs)

    def get(self, kind: Any) -> Any:
        return self._container.get(kind)

    def close(self) -> None:
        pass


def serve(monkeypatch: pytest.MonkeyPatch, control: ControlPlane) -> None:
    """Make the next `create_app` lifespan serve ``control``'s container.

    The API builds its container inside its lifespan, so a test that has
    already seeded a graph hands that one over instead.
    """
    monkeypatch.setattr(
        "blitzecdn.entrypoints.api.app.api_container",
        lambda *_args, **_kwargs: _Borrowed(control.container),
    )


def install_worker(control: ControlPlane) -> Worker:
    """The worker over ``control``'s graph: its queue now has every handler."""
    return control.worker


def run_queued_operations(control: ControlPlane) -> None:
    """Run every queued job the way the worker does, until the queue is empty."""
    queue = control.worker.runner.queue
    while queue.run_one() is not None:
        pass


def advance(
    deployments: Any, deployment_id: str, status: DeploymentStatus, **fields: Any
) -> Deployment:
    """Put a stored deployment into ``status``, the way the service would.

    For setting up a state a test is about; ``fields`` are ``finish``'s
    ``result`` and ``error``.
    """
    current = deployments.get_deployment(deployment_id)
    now = datetime.now(UTC)
    after = (
        current.start(now=now)
        if status is DeploymentStatus.RUNNING
        else current.finish(status, now=now, **fields)
    )
    return cast(Deployment, deployments.save(current, after))


def repository_on(settings: Settings) -> Repository:
    """A second handle on the test database, for seeding and reading back.

    The control plane does not hand out its stores, so a test opens its own handle
    on the same file.
    """
    return Repository(settings.database_path)


def control_plane_app(settings: Settings) -> FastAPI:
    """The all-package workspace API used by cross-capability integration tests."""
    return create_app(settings, features=load_registry())


#: The API key `settings` configures, as the header a client sends.
API_HEADERS = {"X-API-Key": "x" * 32}


def seed_site_over_http(
    client: Any,
    headers: Mapping[str, str] = API_HEADERS,
    *,
    name: str = "example-com",
    label: str = "cdn",
    **policy: object,
) -> Any:
    """Zone, policy, record: over HTTP, the way a client would."""
    client.post("/v3/domains", json={"name": "example.com"}, headers=headers)
    created = client.patch(
        "/v3/domains/example.com",
        json={**policy},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    proxied = client.post(
        "/v3/domains/example.com/records",
        json={
            "name": label,
            "value": "198.51.100.10",
            "proxied": True,
        },
        headers=headers,
    )
    assert proxied.status_code == 201, proxied.text
    return created.json()
