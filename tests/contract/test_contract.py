"""Verify what the control plane emits against what the edge roles declare.

The edge roles live in `ansible/roles/`, in this repository, so these tests read
the roles this control plane actually deploys. Nothing else stops a new
`CdnSite` field from reaching a role that has never heard of it.

Every assertion here is about the boundary between them, not either side alone:

* every key `site_to_ansible()` emits is declared in `argument_specs.yml`;
* every key the role marks required is actually present;
* declared `choices` cover the values the domain enums can produce;
* `site.conf.j2` renders from real model output without raising.

When one of these fails, change the model and the role together in one commit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from contract_support import _role_defaults, _runtime_defaults, _split_runtime
from paths import FIXTURES, REPO_ROOT, optional_packages

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.core.ansible.mapping import site_to_ansible
from blitzecdn.core.database import Repository
from blitzecdn.core.nginx import resolve_nginx_resources
from blitzecdn.core.plugins import load_plugins
from blitzecdn.features.compression.policy import CompressionMode
from blitzecdn.features.dns.domain import DnsRecord, Domain
from blitzecdn.features.http.policy import (
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    HttpScheme,
)
from blitzecdn.features.security.policy import SiteFirewall
from blitzecdn.features.sites.domain import CdnSite, SitePolicy
from blitzecdn.features.sites.policy import CacheQueryStringMode, SiteVisitorHeaders
from blitzecdn.features.tls.policy import (
    CertificateMode,
    MinimumTlsVersion,
    SslAutomaticMode,
    SslMode,
)

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = REPO_ROOT
FIXTURE = FIXTURES / "desired-state.yml"


#: The tests below that read a *capability's* half of the rendered edge
#: configuration, and the capabilities each one needs attached.
#:
#: The rendered server block is composed — core frames it and each installed
#: capability contributes the fragments that fill it — so an assertion on one
#: of those fragments is a contract between two distributions and belongs
#: here, in the one suite that has both installed, rather than in either
#: package's own tests. It cannot hold in the core-only workspace, where the
#: fragment is not there to render, so `just test-core-only` skips exactly
#: these and keeps running every assertion about core's own half.
REQUIRES_CAPABILITIES = {
    "test_http3_is_additive_and_limited_to_udp_443": ("http3",),
    "test_http3_alt_svc_is_in_the_proxy_location_header_set": ("http3",),
    "test_http3_alt_svc_follows_the_listener_not_the_origin": ("http3",),
    "test_default_server_owns_reuseport_once_for_many_http3_sites": ("http3",),
    "test_first_http3_site_owns_reuseport_without_a_catch_all": ("http3",),
    "test_firewall_rules_reach_the_generated_configuration": ("security",),
    "test_the_acme_challenge_path_is_never_filtered": ("geoip", "security"),
    "test_an_allow_country_list_refuses_addresses_the_database_cannot_place": (
        "geoip",
        "security",
    ),
    "test_ip_country_reads_the_variable_the_capability_defines": ("geoip",),
    "test_under_attack_mode_redirects_permanently_on_every_challenge_path": (
        "security",
    ),
    "test_under_attack_mode_renders_before_redirect_and_proxy_on_http_and_https": (
        "security",
    ),
    "test_under_attack_reserved_endpoints_are_edge_only_and_uncached": ("security",),
    "test_acme_bypasses_under_attack_mode_in_every_server_block": ("security",),
    "test_websocket_upgrade_is_forwarded_and_never_cached": ("cache",),
    "test_cache_query_string_mode_selects_the_cache_key": ("cache",),
    "test_the_cache_key_still_separates_the_listener_ports": ("cache",),
    "test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback": ("compression",),
    "test_compression_off_says_so_rather_than_staying_silent": ("compression",),
    "test_gzip_only_turns_the_managed_brotli_filter_off": ("compression",),
    "test_compression_leaves_the_cache_key_and_origin_request_alone": (
        "cache",
        "compression",
    ),
    "test_missing_compression_preserves_pre_upgrade_behavior": ("compression",),
}


def test_ci_actions_are_pinned_to_immutable_commits():
    for workflow in (PROJECT_DIR / ".github/workflows").glob("*.yml"):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "uses:" not in line:
                continue
            reference = line.split("uses:", 1)[1].strip().split()[0]
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                f"{workflow}: action is not pinned to a commit: {reference}"
            )


#: The roles ship with this control plane, so there is no install step to get
#: wrong and no reason for these tests to skip. That matters: they used to read
#: an installed collection and skipped silently when it was absent, which turned
#: a broken contract into a green run.
ROLES_DIR = PROJECT_DIR / "ansible/roles"


def _role(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), f"{name} is missing from ansible/roles/"
    return candidate


ROLE_DIR = _role("blitzecdn_nginx")


def _nginx_resources() -> dict[str, list[dict[str, str]]]:
    return {
        context: [
            {
                "plugin": resource.plugin,
                "name": resource.name,
                "template": str(resource.template),
            }
            for resource in resources
        ]
        for context, resources in resolve_nginx_resources(
            load_plugins().nginx_contributions()
        ).items()
    }


def _nginx_environment():
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    @jinja2.pass_context
    def lookup(context, _plugin, template, *, template_vars=None):
        values = dict(context.get_all())
        values.update(template_vars or {})
        return environment.from_string(Path(template).read_text()).render(**values)

    environment.globals["lookup"] = lookup
    return environment


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _contract(*path: str) -> Any:
    """One member of the resolved edge runtime contract."""
    value: Any = _runtime_defaults()["blitzecdn_edge_runtime"]
    for key in path:
        value = value[key]
    return value


def _defaults_of(role_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8"))


def _capability_defaults() -> dict[str, Any]:
    """Every installed capability role's defaults, the way a play resolves them.

    A capability's fragment reads its own role's variables — the cache zone,
    the compression level — and those are `defaults/main.yml` in a role that
    ships inside the wheel. Discovered through the contributions rather than
    listed by path: a checkout directory is not where an installed controller
    finds them, and naming the packages here would make core's tests need an
    edit every time a capability is attached or detached.
    """
    defaults: dict[str, Any] = {}
    for contribution in load_plugins().ansible_contributions():
        for role in sorted(contribution.roles_path.iterdir()):
            if (role / "defaults/main.yml").is_file():
                defaults |= _defaults_of(role)
    return defaults


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    """Render desired state the way a real deployment would.

    From records, because that is the only way a site comes to exist. The
    snapshot this renders from carries records and derives the sites on read,
    so writing to the derived table here would describe a state no deployment
    can actually produce.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "cdn",
                # An A record's value is an address, so the origin *hostname*
                # travels in origin_request_host and origin_sni instead.
                "value": "198.51.100.20",
                "proxied": True,
                "ssl_mode": SslMode.OFF,
                "origin_request_host": "origin.example.com",
                "origin_sni": "origin.example.com",
                "cache_enabled": True,
                "cache_valid_success": "10m",
                "cache_valid_not_found": "1m",
            }
        )
    )
    repository.zones.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "static",
                "value": "192.0.2.10",
                "proxied": True,
                "ssl_mode": SslMode.FLEXIBLE,
                "enabled": False,
                "cache_enabled": False,
                "certificate_mode": CertificateMode.EXISTING,
                "certificate_path": "/etc/ssl/plain/fullchain.pem",
                "certificate_key_path": "/etc/ssl/plain/privkey.pem",
                # No country rules here on purpose. CI feeds this fixture to a
                # real playbook, where blitzecdn_nginx_geoip_enabled is false
                # and the role is supposed to refuse them. Country rendering is
                # covered against the template directly, below.
                "firewall": {
                    "allow_sources": ["203.0.113.9"],
                    "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                    "denied_methods": ["DELETE", "TRACE"],
                    "denied_paths": ["/admin", "/.git"],
                },
            }
        )
    )
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
    )
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))


def _plays_and_their_roles() -> list[tuple[Path, tuple[Path, ...]]]:
    """Every play in the workspace, with the role directories it may resolve in.

    Core's plays see core's roles. A package's play sees core's *and* its own,
    which is exactly what `resolve_role_search_path` composes at run time — the
    ACME play is the case that matters, because it is owned by
    `blitzecdn-certificates` and names the core `blitzecdn_edge` role.
    """
    found: list[tuple[Path, tuple[Path, ...]]] = [
        (playbook, (ROLES_DIR,))
        for playbook in sorted((PROJECT_DIR / "ansible/playbooks").glob("*.yml"))
    ]
    for package in optional_packages():
        tree = next(package.glob("src/*/ansible"), None)
        if tree is None:
            continue
        search = (ROLES_DIR, tree / "roles") if (tree / "roles").is_dir() else ()
        found.extend(
            (playbook, search or (ROLES_DIR,))
            for playbook in sorted((tree / "playbooks").glob("*.yml"))
        )
    return found


def test_every_role_a_playbook_names_exists():
    """A role rename cannot leave a playbook pointing at a missing local role.

    Across the workspace, not only core: a package's play resolves against the
    same composed search path a deployment gives Ansible, so a capability that
    shipped a play without the role it names fails here.
    """
    referenced: set[str] = set()
    for playbook, search in _plays_and_their_roles():
        document = yaml.safe_load(playbook.read_text(encoding="utf-8"))
        for play in document:
            for entry in play.get("roles", []):
                name = entry["role"] if isinstance(entry, dict) else entry
                referenced.add(name)
                assert any((directory / name).is_dir() for directory in search), (
                    f"{playbook.name} names role {name}, which is in none of "
                    f"{', '.join(str(directory) for directory in search)}"
                )

    assert "blitzecdn_nginx" in referenced, "the sweep found no playbooks to check"
    assert "blitzecdn_cache" in referenced, (
        "the sweep no longer reaches the plays an optional distribution owns"
    )


def test_no_reference_to_the_retired_edge_collection_remains():
    """A stale `blitzecdn.edge.` prefix resolves to nothing and fails at deploy."""
    tracked = [
        *sorted((PROJECT_DIR / "ansible").rglob("*.yml")),
        *sorted((PROJECT_DIR / "ansible").rglob("*.cfg")),
        PROJECT_DIR / "install.sh",
    ]
    offenders = [
        path.relative_to(PROJECT_DIR)
        for path in tracked
        if ".state" not in path.parts
        and "blitzecdn.edge" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"retired collection namespace still referenced in {offenders}"
    )


def test_edge_collection_enforces_public_key_only_ssh():
    """The control plane reaches every edge over SSH and nothing else.

    `ansible/ansible.cfg` refuses to authenticate with anything but a key, and
    the pinned collection is what makes the hosts agree. If a future edge
    release relaxes this drop-in, deploys keep working — the controller still
    has its key — while every edge quietly starts accepting passwords again.
    Nothing else in either repository would notice.
    """
    role = _role("blitzecdn_sshd")
    template = (role / "templates/sshd.conf.j2").read_text(encoding="utf-8")
    directives = {
        line.split()[0].lower(): line.split(maxsplit=1)[1].strip()
        for line in template.splitlines()
        if line and not line.startswith(("#", "{"))
    }
    for keyword, expected in (
        ("pubkeyauthentication", "yes"),
        ("authenticationmethods", "publickey"),
        ("passwordauthentication", "no"),
        ("kbdinteractiveauthentication", "no"),
        ("permitemptypasswords", "no"),
        ("hostbasedauthentication", "no"),
    ):
        assert directives.get(keyword) == expected, (
            f"blitzecdn_sshd no longer sets {keyword} {expected}. Edges "
            "would accept something other than public keys."
        )


def test_edge_ssh_hardening_is_on_by_default():
    """Opting out is possible; arriving opted out by accident is not."""
    defaults = yaml.safe_load(
        (_role("blitzecdn_sshd") / "defaults/main.yml").read_text(encoding="utf-8")
    )
    assert defaults["blitzecdn_sshd_enabled"] is True
    assert defaults["blitzecdn_sshd_permit_root_login"] == "no"


def test_controller_refuses_password_authentication():
    """The other half of the contract: what this repository dials out with."""
    config = (PROJECT_DIR / "ansible/ansible.cfg").read_text(encoding="utf-8")
    for option in (
        "PreferredAuthentications=publickey",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "BatchMode=yes",
    ):
        assert option in config, (
            f"ansible/ansible.cfg no longer passes -o {option}, so a deploy "
            "could authenticate to an edge with a password."
        )
    assert "host_key_checking = True" in config


def test_empty_site_removal_requires_explicit_approval(desired_state):
    assert desired_state["blitzecdn_nginx_allow_empty_sites"] is False
    assert "blitzecdn_nginx_allow_empty_sites" in _role_spec()


def test_every_emitted_key_is_declared_by_the_role(desired_state):
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"])
    # The control plane adds these when distributing managed certificates.
    declared |= {"certificate_source_path", "certificate_key_source_path"}
    for site in desired_state["blitzecdn_nginx_sites"]:
        undeclared = set(site) - declared
        assert not undeclared, (
            f"CdnSite emits {sorted(undeclared)}, which the local "
            "roles/blitzecdn_nginx/meta/argument_specs.yml does not declare."
        )


def test_required_keys_are_always_emitted(desired_state):
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    required = {name for name, spec in options.items() if (spec or {}).get("required")}
    for site in desired_state["blitzecdn_nginx_sites"]:
        missing = required - set(site)
        assert not missing, (
            f"site {site.get('name')!r} omits required {sorted(missing)}"
        )


def test_nginx_role_accepts_only_the_current_ssl_policy():
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    assert options["ssl_mode"]["required"] is True
    assert "origin_scheme" not in options


@pytest.mark.parametrize(
    ("field", "enum"),
    [
        ("ssl_mode", SslMode),
        ("ssl_automatic_mode", SslAutomaticMode),
        ("minimum_tls_version", MinimumTlsVersion),
        ("certificate_mode", CertificateMode),
        ("cache_query_string_mode", CacheQueryStringMode),
        ("compression", CompressionMode),
    ],
)
def test_role_choices_cover_every_domain_value(field, enum):
    """A new enum member must not reach a role that rejects it."""
    declared = set(_role_spec()["blitzecdn_nginx_sites"]["options"][field]["choices"])
    assert {member.value for member in enum} <= declared, (
        f"{enum.__name__} has values the role's {field} choices do not allow"
    )


def test_every_emitted_firewall_key_is_declared_by_the_role(desired_state):
    """The outer check only sees top-level keys; the firewall is nested.

    Role argument validation rejects an undeclared suboption, so a new
    ``SiteFirewall`` field reaching an older role fails the deploy rather than
    being ignored — which is the right failure, but only if it is caught here
    first.
    """
    declared = set(
        _role_spec()["blitzecdn_nginx_sites"]["options"]["firewall"]["options"]
    )
    assert set(SiteFirewall.model_fields) == declared, (
        "SiteFirewall and the role's firewall suboptions disagree: "
        f"{sorted(set(SiteFirewall.model_fields) ^ declared)}"
    )
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site.get("firewall", {})) <= declared


def test_public_ports_match_cloudflare_and_the_firewall():
    """A listener without a firewall rule is unreachable, and the reverse
    exposes a port that can never serve traffic.

    Both roles read one contract member now, so the two cannot fall out of
    lockstep — what is still worth pinning is that the member holds the
    Cloudflare-compatible proxy port sets, and that the domain's copy agrees.
    """
    http_ports = [80, 8080, 8880, 2052, 2082, 2086, 2095]
    https_ports = [443, 2053, 2083, 2087, 2096, 8443]

    assert _contract("listeners", "http") == http_ports
    assert _contract("listeners", "https") == https_ports
    assert _contract("listeners", "http3") is False
    # The domain holds the second copy, because Flexible's origin scheme now
    # depends on which set a listener belongs to. It is the last one: the Nginx
    # role binds these ports and the firewall opens them from the same contract
    # member, so a listener without a rule is no longer possible to write.
    assert list(HTTP_PROXY_PORTS) == http_ports
    assert list(HTTPS_PROXY_PORTS) == https_ports
    firewall = _defaults_of(_role("blitzecdn_firewall"))
    assert "blitzecdn_firewall_http_ports" not in firewall
    assert "blitzecdn_firewall_http3_enabled" not in firewall


def test_default_server_claims_every_public_listener():
    """Unknown hostnames must not fall through to a customer site on any port."""
    environment = _nginx_environment()
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = environment.get_template("default.conf.j2").render(**defaults)

    for port in runtime["listeners"]["http"]:
        assert f"listen {port} default_server;" in rendered
        assert f"listen [::]:{port} default_server;" in rendered
    for port in runtime["listeners"]["https"]:
        assert f"listen {port} ssl default_server;" in rendered
        assert f"listen [::]:{port} ssl default_server;" in rendered


def _render(site: dict[str, Any], **overrides: Any) -> str:
    environment = _nginx_environment()
    # A contract input has to be applied before the contract is composed, so
    # `blitzecdn_edge_geoip_enabled=True` reaches the template as
    # `blitzecdn_edge_runtime.geoip.enabled`, not as a stray extra variable.
    inputs, plain = _split_runtime(overrides)
    return environment.get_template("site.conf.j2").render(
        **(
            _role_defaults(**inputs)
            | _capability_defaults()
            | {"blitzecdn_nginx_resources": _nginx_resources()}
            | plain
        ),
        item=site,
    )


def _http3_site(name: str = "quic") -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": name,
                "server_names": [f"{name}.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "http3_enabled": True,
                "minimum_tls_version": "1.2",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        )
    )


def test_http3_is_additive_and_limited_to_udp_443():
    rendered = _render(_http3_site())

    assert "listen 443 ssl;" in rendered
    assert "http2 on;" in rendered
    assert "listen 443 quic;" in rendered
    assert "listen [::]:443 quic;" in rendered
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in rendered
    for port in (2053, 2083, 2087, 2096, 8443):
        assert f"listen {port} quic" not in rendered
        assert f"listen [::]:{port} quic" not in rendered


def test_http3_alt_svc_is_in_the_proxy_location_header_set():
    rendered = _render(_http3_site())
    assert rendered.count("add_header Alt-Svc") == 1
    assert "add_header Alt-Svc 'h3=\":443\"; ma=86400' always;" in rendered
    location = rendered[rendered.index("location / {") :]
    assert "add_header Alt-Svc" in location


def test_http3_disabled_emits_neither_quic_nor_alt_svc():
    site = _http3_site()
    site["http3_enabled"] = False
    rendered = _render(site)
    assert "listen 443 quic" not in rendered
    assert "Alt-Svc" not in rendered


def test_default_server_owns_reuseport_once_for_many_http3_sites():
    defaults = _role_defaults(blitzecdn_edge_http3_enabled=True)
    defaults["blitzecdn_nginx_resources"] = _nginx_resources()
    environment = _nginx_environment()
    catch_all = environment.get_template("default.conf.j2").render(**defaults)
    sites = "".join(_render(_http3_site(name)) for name in ("alpha", "bravo"))

    assert catch_all.count("quic reuseport default_server") == 2
    assert "reuseport" not in sites
    assert sites.count("listen 443 quic;") == 2
    assert "ssl_reject_handshake on;" in catch_all


def test_first_http3_site_owns_reuseport_without_a_catch_all():
    overrides = {
        "blitzecdn_nginx_default_server": False,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
    alpha = _render(_http3_site("alpha"), **overrides)
    bravo = _render(_http3_site("bravo"), **overrides)

    assert alpha.count("quic reuseport;") == 2
    assert "reuseport" not in bravo


def test_desired_state_states_http3_once_for_the_firewall_and_the_listener(
    settings, tmp_path
):
    """The shape of the QUIC contract, which does not depend on what is installed.

    One key, not two. The firewall's UDP/443 rule and the QUIC listener read the
    same contract member, so desired state states HTTP/3 once and the edge play
    no longer has to assert that two copies of it agree.

    Both keys are `required: true` in the edge and Nginx argument specs, so they
    are emitted whether or not `blitzecdn-http3` is attached — core writes the
    baseline and the package overrides it. The *values* are that package's
    behavior and are asserted in its own tests and in the packaging lifecycle;
    what core owns is that these two names, and no others, carry the answer.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    for name in ("zeta", "alpha"):
        repository.zones.create_record(
            DnsRecord.model_validate(
                {
                    "domain": "example.com",
                    "name": name,
                    "value": "198.51.100.20",
                    "proxied": True,
                    "ssl_mode": "flexible",
                    "http3_enabled": True,
                    "certificate_mode": "existing",
                    "certificate_path": "/etc/ssl/certs/edge.pem",
                    "certificate_key_path": "/etc/ssl/private/edge.key",
                }
            )
        )
    output = tmp_path / "http3.yml"
    control.deployments.write_desired_state(repository.snapshot(), output)
    document = yaml.safe_load(output.read_text(encoding="utf-8"))

    assert isinstance(document["blitzecdn_edge_http3_enabled"], bool)
    assert isinstance(document["blitzecdn_nginx_http3_listener_owner"], str)
    assert "blitzecdn_nginx_http3_enabled" not in document
    assert "blitzecdn_firewall_http3_enabled" not in document


def test_firewall_rules_reach_the_generated_configuration(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry.get("firewall")
    )
    rendered = _render(site)
    assert "allow 203.0.113.9/32;" in rendered
    assert "deny 203.0.113.0/24;" in rendered
    assert "deny 2001:db8::/32;" in rendered
    assert 'if ($request_method ~ "^(DELETE|TRACE)$")' in rendered
    assert "location ^~ /admin {" in rendered
    # The allow has to precede the denies: nginx takes the first match, so the
    # reverse order would make every exemption dead.
    assert rendered.index("allow 203.0.113.9/32;") < rendered.index(
        "deny 203.0.113.0/24;"
    )


def test_a_site_without_firewall_rules_renders_exactly_as_before(desired_state):
    """Every existing site must be untouched by the new block."""
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if not entry.get("firewall")
    )
    rendered = _render(site)
    for directive in ("allow ", "deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in rendered


def test_the_acme_challenge_path_is_never_filtered():
    """A rule that blocked renewal would surface weeks later, at expiry."""
    site = site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "locked",
                "server_names": ["locked.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {
                    "deny_sources": ["0.0.0.0/0", "::/0"],
                    "denied_countries": ["RU"],
                    "denied_methods": ["GET"],
                },
            }
        )
    )
    rendered = _render(site, blitzecdn_edge_geoip_enabled=True)
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    challenge_block = rendered[challenge:block_end]
    for directive in ("deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in challenge_block, (
            f"{directive!r} applies to the ACME challenge location, so a site "
            "can filter out its own certificate authority"
        )
    # …while the site itself really is closed.
    assert "deny 0.0.0.0/0;" in rendered
    assert 'if ($blitzecdn_country ~ "^(RU)$")' in rendered


def test_an_allow_country_list_refuses_addresses_the_database_cannot_place():
    """`""` is what geoip2 yields for an unknown address.

    An allow list has to treat it as "not one of these"; the negated match is
    the only form that does. A positive match on a denied list, conversely,
    must not fire — that asymmetry is deliberate and easy to invert by
    accident.
    """
    site = site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "geo",
                "server_names": ["geo.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {"allowed_countries": ["DE", "FR"]},
            }
        )
    )
    rendered = _render(site, blitzecdn_edge_geoip_enabled=True)
    assert 'if ($blitzecdn_country !~ "^(DE|FR)$")' in rendered


def _with_visitor_headers(**headers: bool) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "visitor",
                "server_names": ["visitor.example.com"],
                "origin_host": "origin.example.com",
                "visitor_headers": headers,
            }
        )
    )


def test_every_emitted_visitor_header_key_is_declared_by_the_role(desired_state):
    """Nested like the firewall, and invisible to the top-level key sweep."""
    declared = set(
        _role_spec()["blitzecdn_nginx_sites"]["options"]["visitor_headers"]["options"]
    )
    assert set(SiteVisitorHeaders.model_fields) == declared, (
        "SiteVisitorHeaders and the role's visitor_headers suboptions disagree: "
        f"{sorted(set(SiteVisitorHeaders.model_fields) ^ declared)}"
    )
    for site in desired_state["blitzecdn_nginx_sites"]:
        assert set(site["visitor_headers"]) == declared


def test_the_role_defaults_agree_with_the_domain_defaults():
    """An edge upgraded ahead of its controller must behave the same.

    The role's suboption defaults apply when an older control plane sends no
    visitor_headers at all, so they have to be the values the domain would have
    sent.
    """
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]["visitor_headers"]
    assert options.get("required", False) is False
    for name, field in SiteVisitorHeaders.model_fields.items():
        assert options["options"][name]["default"] == field.default


def test_the_default_site_sends_the_address_and_not_the_country(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry["name"] == "cdn-example-com"
    )
    rendered = _render(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert "$blitzecdn_country" not in rendered


def test_connecting_ip_enabled_sends_the_nginx_connection_address():
    """`$remote_addr`, and nothing a client can write.

    The peer address of the accepted connection is the only value at the edge
    that a request header cannot influence, and it renders identically for IPv4
    and IPv6 — nginx fills the variable from the socket.
    """
    rendered = _render(_with_visitor_headers(connecting_ip=True))

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    for forgeable in (
        "$http_x_forwarded_for",
        "$http_x_real_ip",
        "$http_bz_connecting_ip",
        "$http_true_client_ip",
        "$http_cf_connecting_ip",
    ):
        assert forgeable not in rendered


def test_connecting_ip_disabled_clears_the_header_rather_than_forwarding_it():
    """Off must not mean "pass the visitor's own version through".

    nginx forwards request headers it was not told about, so omitting the
    directive would hand the origin a BZ-Connecting-IP the client wrote. An
    empty value is dropped from the upstream request and takes the client's
    header with it.
    """
    rendered = _render(_with_visitor_headers(connecting_ip=False))

    assert 'proxy_set_header BZ-Connecting-IP "";' in rendered
    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" not in rendered


def test_ip_country_reads_the_variable_the_capability_defines():
    """The same $blitzecdn_country the firewall rules test, not a second lookup.

    Core's half only. The `geoip2` block that *defines* the variable is the
    `blitzecdn-geoip` distribution's, and that both sides spell the name the
    same way is asserted in that package's own tests, which can read both.
    Repeating it here would make this suite fail on a checkout where the
    optional distribution is not installed.
    """
    rendered = _render(_with_visitor_headers(ip_country=True))

    assert "set $blitzecdn_visitor_ip_country $blitzecdn_country;" in rendered
    assert "proxy_set_header BZ-IPCountry $blitzecdn_visitor_ip_country;" in rendered
    assert rendered.count("geoip2") == 0


def test_ip_country_disabled_clears_the_header_rather_than_forwarding_it():
    rendered = _render(_with_visitor_headers(ip_country=False))

    assert 'set $blitzecdn_visitor_ip_country "";' in rendered
    assert "$blitzecdn_visitor_ip_country $blitzecdn_country" not in rendered
    assert "$blitzecdn_country" not in rendered


def test_a_spoofed_bz_header_is_replaced_in_every_state():
    """Whatever the switches say, the BZ- namespace is written by the edge.

    Both directives are emitted unconditionally — one carries a value, the
    other clears the name — so a client-supplied BZ-Connecting-IP or
    BZ-IPCountry can never reach the origin unmodified.
    """
    for headers in (
        {"connecting_ip": True, "ip_country": True},
        {"connecting_ip": True, "ip_country": False},
        {"connecting_ip": False, "ip_country": True},
        {"connecting_ip": False, "ip_country": False},
    ):
        rendered = _render(
            _with_visitor_headers(**headers), blitzecdn_edge_geoip_enabled=True
        )
        for header in ("BZ-Connecting-IP", "BZ-IPCountry"):
            # Once per server block: the HTTP listeners, and no TLS here.
            emitted = rendered.count(f"proxy_set_header {header} ")
            assert emitted == len(_contract("listeners", "http")), (
                f"{header} is not written on every listener for {headers}"
            )


def test_visitor_headers_never_reach_the_cache_key():
    """Two visitors from different countries share one cached object.

    Putting either header in the key would multiply every entry by the number
    of distinct client addresses, which is the whole cache.
    """
    rendered = _render(
        _with_visitor_headers(connecting_ip=True, ip_country=True),
        blitzecdn_edge_geoip_enabled=True,
    )

    for line in rendered.splitlines():
        if "proxy_cache_key" in line:
            assert "$remote_addr" not in line
            assert "$blitzecdn_country" not in line
            assert "BZ-" not in line


def test_visitor_headers_do_not_disturb_the_existing_forwarding_headers():
    """X-Real-IP and X-Forwarded-For keep their pre-feature behaviour."""
    rendered = _render(_with_visitor_headers())

    assert "proxy_set_header X-Real-IP $remote_addr;" in rendered
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in rendered
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in rendered


def test_the_acme_challenge_location_carries_no_visitor_headers():
    """It is served from disk; there is no origin request to annotate."""
    rendered = _render(
        _with_visitor_headers(connecting_ip=True, ip_country=True),
        blitzecdn_edge_geoip_enabled=True,
    )
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))

    assert "BZ-" not in rendered[challenge:block_end]


def test_missing_visitor_headers_preserve_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role.

    The role's defaults apply, which means the visitor address is sent and the
    country is not — exactly what both sides do once the controller catches up.
    """
    site = _with_visitor_headers()
    del site["visitor_headers"]

    rendered = _render(site)

    assert "proxy_set_header BZ-Connecting-IP $remote_addr;" in rendered
    assert 'set $blitzecdn_visitor_ip_country "";' in rendered


def test_site_template_renders_from_real_model_output(desired_state):
    """Catches template breakage that --syntax-check cannot see."""
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    context = _role_defaults()
    for site in desired_state["blitzecdn_nginx_sites"]:
        rendered = environment.get_template("site.conf.j2").render(**context, item=site)
        assert f"server_name {' '.join(site['server_names'])};" in rendered
        assert "proxy_pass" in rendered
        if site["certificate_mode"] == CertificateMode.DISABLED:
            assert "ssl_certificate" not in rendered
        else:
            assert f"ssl_certificate {site['certificate_path']};" in rendered


def test_the_template_sends_the_sni_the_control_plane_probed_with():
    """Both halves must resolve SNI identically, and never to a wildcard.

    `OriginProbe` verifies the origin certificate against
    `CdnSite.effective_origin_sni`; the edge is what actually sends it. If they
    drift, a preflight pass means nothing. A wildcard `server_name` is the case
    that used to break this: legal in nginx, unmatchable in a handshake.
    """
    site = CdnSite.model_validate(
        {
            "name": "wildcard",
            "server_names": ["*.example.com", "example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": SslMode.FULL_STRICT,
            "certificate_mode": CertificateMode.EXISTING,
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )
    assert site.effective_origin_sni == "origin.example.com"
    rendered = _render(site_to_ansible(site))
    assert "proxy_ssl_name origin.example.com;" in rendered
    assert "proxy_ssl_verify on;" in rendered
    assert (
        "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;" in rendered
    )
    assert "proxy_ssl_verify_depth 5;" in rendered


def _server_blocks(
    rendered: str, defaults: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Split a rendered site into its HTTP and HTTPS ``server`` blocks.

    Directives have to be attributed to the listener they belong to: "somewhere
    in the file" is exactly the assertion that let an origin TLS directive sit
    in a plaintext location unnoticed.
    """
    runtime = defaults["blitzecdn_edge_runtime"]
    blocks = ["server {" + part for part in rendered.split("server {")[1:]]
    http = [
        block
        for block in blocks
        if any(f"listen {port};" in block for port in runtime["listeners"]["http"])
    ]
    https = [
        block
        for block in blocks
        if any(f"listen {port} ssl;" in block for port in runtime["listeners"]["https"])
    ]
    assert len(http) + len(https) == len(blocks), "a server block matched neither set"
    return http, https


def _mode_site(mode: SslMode, serves_tls: bool, **extra: Any) -> CdnSite:
    payload: dict[str, Any] = {
        "name": "mode",
        "server_names": ["mode.example.com"],
        "origin_host": "origin.example.com",
        "ssl_mode": mode,
    }
    if serves_tls:
        payload |= {
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    return CdnSite.model_validate(payload | extra)


#: (mode, serves_tls, origin scheme on an HTTP listener, origin scheme on the
#: canonical HTTPS listener :443, origin scheme on an alternate HTTPS listener).
#:
#: The origin *port* is not in here on purpose: it is always the listener's own,
#: for every mode and every one of the thirteen public proxy ports. The scheme
#: varies with the visitor — Full and Full (strict) mirror the visitor rather
#: than forcing HTTPS, so an HTTP request under Full still reaches an HTTP
#: origin — and, for Flexible alone, with the listener port: Flexible is
#: Flexible on 443 and falls back to Full-like transport on 2053/2083/2087/
#: 2096/8443.
_MODE_SCHEMES = [
    (SslMode.OFF, False, "http", None, None),
    (SslMode.FLEXIBLE, True, "http", "http", "https"),
    (SslMode.FULL, True, "http", "https", "https"),
    (SslMode.FULL_STRICT, True, "http", "https", "https"),
]


def _expected_upstreams(
    defaults: dict[str, Any],
    serves_tls: bool,
    http_origin: str,
    canonical_origin: str | None,
    alternate_origin: str | None,
    host: str = "origin.example.com",
) -> set[str]:
    """Every upstream a site in one mode must emit, keyed by listener."""
    runtime = defaults["blitzecdn_edge_runtime"]
    expected = {
        f"{http_origin}://{host}:{port}" for port in runtime["listeners"]["http"]
    }
    if serves_tls:
        for port in runtime["listeners"]["https"]:
            scheme = canonical_origin if port == 443 else alternate_origin
            expected.add(f"{scheme}://{host}:{port}")
    return expected


def _upstreams(rendered: str) -> set[str]:
    return set(re.findall(r"set \$blitzecdn_upstream (\S+);", rendered))


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_every_ssl_mode_renders_its_transport(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """Every listener proxies to its own port, over the mode's scheme for it."""
    rendered = _render(site_to_ansible(_mode_site(mode, serves_tls)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    for port in runtime["listeners"]["http"]:
        assert f"listen {port};" in rendered
        assert f"listen [::]:{port};" in rendered
    for port in runtime["listeners"]["https"]:
        assert (f"listen {port} ssl;" in rendered) is serves_tls
        assert (f"listen [::]:{port} ssl;" in rendered) is serves_tls

    assert _upstreams(rendered) == _expected_upstreams(
        defaults, serves_tls, http_origin, canonical_origin, alternate_origin
    )
    assert "return 301 https://$host$request_uri;" not in rendered


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_the_visitor_port_is_preserved_toward_the_origin(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """The whole feature, stated once: origin port == listener port.

    A request to :8080 must reach the origin's 8080, not its 80 — the bug this
    replaces sent every alternate port to the scheme's default.
    """
    rendered = _render(site_to_ansible(_mode_site(mode, serves_tls)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    ports = {int(upstream.rsplit(":", 1)[1]) for upstream in _upstreams(rendered)}
    listeners = set(runtime["listeners"]["http"])
    if serves_tls:
        listeners |= set(runtime["listeners"]["https"])
    assert ports == listeners


#: Representative alternate ports plus the canonical pair. One case per row of
#: the SSL-mode matrix, spelled out as the literal upstream the edge must emit.
_PORT_UPSTREAMS = [
    (SslMode.OFF, False, 80, "http://origin.example.com:80"),
    (SslMode.OFF, False, 8080, "http://origin.example.com:8080"),
    (SslMode.OFF, False, 2052, "http://origin.example.com:2052"),
    (SslMode.FLEXIBLE, True, 8080, "http://origin.example.com:8080"),
    # Flexible is Flexible on 443 and Full-like on every other HTTPS port.
    (SslMode.FLEXIBLE, True, 443, "http://origin.example.com:443"),
    (SslMode.FLEXIBLE, True, 2053, "https://origin.example.com:2053"),
    (SslMode.FLEXIBLE, True, 2083, "https://origin.example.com:2083"),
    (SslMode.FLEXIBLE, True, 2087, "https://origin.example.com:2087"),
    (SslMode.FLEXIBLE, True, 2096, "https://origin.example.com:2096"),
    (SslMode.FLEXIBLE, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL, True, 80, "http://origin.example.com:80"),
    (SslMode.FULL, True, 8080, "http://origin.example.com:8080"),
    (SslMode.FULL, True, 443, "https://origin.example.com:443"),
    (SslMode.FULL, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL_STRICT, True, 2052, "http://origin.example.com:2052"),
    (SslMode.FULL_STRICT, True, 8443, "https://origin.example.com:8443"),
    (SslMode.FULL_STRICT, True, 2053, "https://origin.example.com:2053"),
]


@pytest.mark.parametrize(("mode", "serves_tls", "port", "upstream"), _PORT_UPSTREAMS)
def test_representative_ports_render_their_own_upstream(
    mode, serves_tls, port, upstream
):
    rendered = _render(site_to_ansible(_mode_site(mode, serves_tls)))
    assert upstream in _upstreams(rendered)


def test_flexible_443_is_plaintext_and_flexible_8443_is_not():
    """The compatibility bug this change exists to fix, pinned on its own.

    Treating Flexible as one global origin protocol sent an HTTPS visitor on
    8443 to ``http://origin:8443``. Cloudflare supports Flexible for HTTPS on
    443 only; the other five HTTPS proxy ports fall back to Full.
    """
    upstreams = _upstreams(_render(site_to_ansible(_mode_site(SslMode.FLEXIBLE, True))))

    assert "http://origin.example.com:443" in upstreams
    assert "https://origin.example.com:443" not in upstreams
    assert "https://origin.example.com:8443" in upstreams
    assert "http://origin.example.com:8443" not in upstreams


def _https_block(rendered: str, defaults: dict[str, Any], port: int) -> str:
    """The one HTTPS server block listening on ``port``."""
    _, https_blocks = _server_blocks(rendered, defaults)
    matching = [block for block in https_blocks if f"listen {port} ssl;" in block]
    assert len(matching) == 1, f"expected exactly one :{port} block"
    return matching[0]


def test_flexible_emits_origin_tls_only_on_the_fallback_listeners():
    """443 terminates and re-originates plaintext; the alternates do not.

    proxy_ssl_* on a plaintext leg would be a claim the edge does not honour, so
    the directives have to be attributed to the listener rather than to the site.
    """
    rendered = _render(site_to_ansible(_mode_site(SslMode.FLEXIBLE, True)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, _ = _server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_" not in block
    assert "proxy_ssl_" not in _https_block(rendered, defaults, 443)

    for port in runtime["listeners"]["https"]:
        if port == 443:
            continue
        block = _https_block(rendered, defaults, port)
        # Full-like transport and SNI, but never Full (strict)'s verification:
        # an origin that opted into Flexible was never asked for a certificate
        # the edge could validate.
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_name origin.example.com;" in block
        assert "proxy_ssl_verify off;" in block
        assert "proxy_ssl_trusted_certificate" not in block


def test_full_strict_verifies_on_every_https_listener_including_alternates():
    rendered = _render(site_to_ansible(_mode_site(SslMode.FULL_STRICT, True)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    for port in runtime["listeners"]["https"]:
        block = _https_block(rendered, defaults, port)
        assert f"https://origin.example.com:{port}" in block
        assert "proxy_ssl_verify on;" in block
        assert "proxy_ssl_server_name on;" in block


def test_ssl_off_never_encrypts_an_origin_leg_on_any_port():
    rendered = _render(site_to_ansible(_mode_site(SslMode.OFF, serves_tls=False)))
    assert "proxy_ssl_" not in rendered
    assert "https://origin.example.com" not in rendered


@pytest.mark.parametrize(
    ("mode", "serves_tls", "http_origin", "canonical_origin", "alternate_origin"),
    _MODE_SCHEMES,
)
def test_the_template_agrees_with_the_domains_scheme_rule(
    mode, serves_tls, http_origin, canonical_origin, alternate_origin
):
    """The rule is written twice — Jinja and Python — so pin them together.

    ``site.conf.j2`` cannot import ``SslMode``, so the only defence against the
    two copies drifting is asserting the rendered output against the domain
    method for every mode and every listener. That now includes the port, which
    is the whole of Flexible's alternate-port fallback: an equality on the
    complete upstream set, not a containment check, so a template that answered
    HTTPS where the domain answers HTTP would fail here too.
    """
    rendered = _render(site_to_ansible(_mode_site(mode, serves_tls)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    listeners = [(HttpScheme.HTTP, port) for port in runtime["listeners"]["http"]]
    if serves_tls:
        listeners += [
            (HttpScheme.HTTPS, port) for port in runtime["listeners"]["https"]
        ]
    assert _upstreams(rendered) == {
        f"{mode.origin_scheme_for(visitor_scheme, port).value}"
        f"://origin.example.com:{port}"
        for visitor_scheme, port in listeners
    }


def test_full_strict_verifies_only_where_the_origin_leg_is_https():
    """TLS directives belong to the HTTPS listeners and nowhere else.

    Under Full (strict) an HTTP visitor is proxied over HTTP, so its location
    must carry no proxy_ssl_* directive at all — verification of a connection
    that is not TLS is meaningless, and emitting it would be a claim the edge
    does not honour.
    """
    rendered = _render(site_to_ansible(_mode_site(SslMode.FULL_STRICT, True)))
    defaults = _role_defaults()
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_verify" not in block
        assert "proxy_ssl_server_name" not in block
        assert "proxy_ssl_name" not in block
        assert "proxy_ssl_trusted_certificate" not in block
    for block in https_blocks:
        assert "proxy_ssl_verify on;" in block
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_name origin.example.com;" in block
        assert (
            "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;" in block
        )
        assert "proxy_ssl_verify_depth 5;" in block


def test_full_does_not_verify_and_still_sends_sni_on_https_listeners():
    rendered = _render(site_to_ansible(_mode_site(SslMode.FULL, True)))
    defaults = _role_defaults()
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

    for block in http_blocks:
        assert "proxy_ssl_" not in block
    for block in https_blocks:
        assert "proxy_ssl_verify off;" in block
        assert "proxy_ssl_server_name on;" in block
        assert "proxy_ssl_trusted_certificate" not in block


def test_an_ipv6_origin_is_bracketed_on_every_listener_port():
    """The literal has to keep its brackets once a port is appended to it."""
    site = _mode_site(SslMode.FULL, serves_tls=True, origin_host="2001:db8::10")
    rendered = _render(site_to_ansible(site))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]

    assert _upstreams(rendered) == {
        f"http://[2001:db8::10]:{port}" for port in runtime["listeners"]["http"]
    } | {f"https://[2001:db8::10]:{port}" for port in runtime["listeners"]["https"]}
    # No unbracketed form anywhere, which would parse as host 2001 port db8.
    assert "//2001:db8::10:" not in rendered


def test_a_pinned_resolver_free_edge_still_preserves_the_listener_port():
    """Without resolvers the upstream is a literal proxy_pass, same rule."""
    rendered = _render(
        site_to_ansible(_mode_site(SslMode.FULL, serves_tls=True)),
        blitzecdn_nginx_resolvers=[],
    )
    assert "set $blitzecdn_upstream" not in rendered
    passes = set(re.findall(r"proxy_pass (\S+);", rendered))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    assert passes == {
        f"http://origin.example.com:{port}" for port in runtime["listeners"]["http"]
    } | {f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]}


def test_the_origin_request_host_override_survives_port_preservation():
    site = _mode_site(SslMode.FULL, serves_tls=True, origin_request_host="app.internal")
    rendered = _render(site_to_ansible(site))
    assert "proxy_set_header Host app.internal;" in rendered
    assert "http://origin.example.com:8080" in rendered
    assert "https://origin.example.com:8443" in rendered


def test_the_cache_key_still_separates_the_listener_ports():
    """$server_port is what keeps :8080 and :80 from sharing a cached object.

    Preserving the port toward the origin makes this load-bearing rather than
    merely tidy: two listeners can now reach genuinely different origin
    services.
    """
    rendered = _render(site_to_ansible(_mode_site(SslMode.FULL, True)))
    assert rendered.count('proxy_cache_key "$scheme$server_port$request_method') >= 2


def test_http3_alt_svc_follows_the_listener_not_the_origin():
    """Alt-Svc advertises the edge's own :443 on the :443 listener only."""
    site = _mode_site(SslMode.FLEXIBLE, serves_tls=True, http3_enabled=True)
    rendered = _render(site_to_ansible(site))
    assert "http://origin.example.com:443" in rendered
    assert rendered.count("add_header Alt-Svc 'h3=\":443\"; ma=86400' always;") == 1


def test_the_acme_challenge_path_never_proxies_on_any_listener():
    """Challenge files are served from disk, so no listener turns one into an
    origin request on its own port."""
    rendered = _render(site_to_ansible(_mode_site(SslMode.FULL, True)))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    expected = len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])
    assert rendered.count("location ^~ /.well-known/acme-challenge/ {") == expected
    for block in re.findall(
        r"location \^~ /\.well-known/acme-challenge/ \{(.*?)\n    \}",
        rendered,
        re.S,
    ):
        assert "proxy_pass" not in block
        assert f"root {runtime['paths']['acme']};" in block


def test_origin_port_is_not_part_of_the_edge_site_contract():
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    assert "origin_port" not in SitePolicy.model_fields
    assert "origin_port" not in options


def test_always_use_https_redirects_http_when_enabled():
    site = CdnSite.model_validate(
        {
            "name": "redirect",
            "server_names": ["redirect.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "always_use_https": True,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    rendered = _render(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" in rendered


def test_always_use_https_uses_a_permanent_redirect():
    """Cloudflare-compatible status code: 301, not 308.

    308 preserves the request method, which is the safer redirect in general and
    the wrong one here — Cloudflare answers Always Use HTTPS with a permanent
    301, and a client that follows it differently from a Cloudflare-fronted
    origin is a compatibility difference the operator cannot see. Every path
    that redirects — the plain HTTP location, both Under Attack Mode challenge
    endpoints, and the named redirect location — uses the same code, so the
    template is checked whole rather than one rendering at a time.
    """
    template = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    security_template = (
        REPO_ROOT
        / "packages/blitzecdn-security/src/blitzecdn_security/nginx"
        / "security-server.conf.j2"
    ).read_text(encoding="utf-8")
    implementation = template + security_template

    assert "return 30" in implementation
    assert "return 308" not in implementation
    assert implementation.count("return 301 https://$host$request_uri;") == 3


def test_always_use_https_redirects_every_http_listener_without_its_port():
    """Redirect semantics are unchanged by port preservation, deliberately.

    The thirteen proxy ports are two independent sets, not seven pairs: 8080's
    counterpart is not 8443, and Cloudflare publishes no mapping between them.
    Carrying an HTTP-only port such as 8080 or 2052 into the Location would
    invent one and send the visitor to an endpoint the edge does not serve, so
    the redirect stays scheme-only. ``$host`` carries no port, which is what
    makes every HTTP listener land on the default 443 — a listener the site
    always serves once it serves TLS at all, so the redirect cannot loop.
    """
    site = _mode_site(SslMode.FULL, serves_tls=True, always_use_https=True)
    rendered = _render(site_to_ansible(site))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

    ports = runtime["listeners"]["http"] + runtime["listeners"]["https"]
    assert len(http_blocks) == len(runtime["listeners"]["http"])
    for block in http_blocks:
        assert "return 301 https://$host$request_uri;" in block
        assert "proxy_pass" not in block
        assert "$blitzecdn_upstream" not in block
        # No port is carried into the Location, from either set.
        for port in ports:
            assert f"https://$host:{port}" not in block
    # The HTTPS listeners still proxy, each to its own port.
    assert _upstreams(rendered) == {
        f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]
    }
    assert len(https_blocks) == len(runtime["listeners"]["https"])


def _redirects(rendered: str) -> bool:
    return "return 301 https://$host$request_uri;" in rendered


@pytest.mark.parametrize("under_attack", [False, True])
@pytest.mark.parametrize(
    "mode", [SslMode.OFF, SslMode.FLEXIBLE, SslMode.FULL, SslMode.FULL_STRICT]
)
def test_the_template_agrees_with_the_domains_redirect_rule(mode, under_attack):
    """The second rule written twice, pinned the same way as the first.

    ``always_use_https`` is inert unless the site serves TLS, and the template
    gates on ``tls and always_use_https`` while the control plane answers
    ``CdnSite.redirects_http_to_https``. Assert the rendering against the
    property for every mode, in both the normal and the Under Attack Mode
    request flow, so neither copy can move alone.
    """
    site = _mode_site(
        mode,
        serves_tls=mode is not SslMode.OFF,
        always_use_https=True,
        under_attack_mode=under_attack,
    )
    rendered = _render(
        site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=under_attack
    )

    assert _redirects(rendered) is site.redirects_http_to_https


def test_ssl_off_ignores_always_use_https_instead_of_looping():
    """Off serves no HTTPS listener, so the redirect must not be emitted.

    Cloudflare removes the Always Use HTTPS control from the dashboard while the
    encryption mode is Off. BlitzeCDN keeps the stored preference — the record
    API still accepts the combination, in either order — and renders it inert,
    which is the only outcome that cannot send a visitor to a port the edge does
    not answer on. A permanent 301 to a dead port is worse than a temporary one:
    browsers cache it.
    """
    site = _mode_site(SslMode.OFF, serves_tls=False, always_use_https=True)
    rendered = _render(site_to_ansible(site))
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, https_blocks = _server_blocks(rendered, defaults)

    assert site.always_use_https is True
    assert site.redirects_http_to_https is False
    assert not https_blocks
    assert "listen 443" not in rendered
    assert not _redirects(rendered)
    # Every HTTP listener still proxies, over HTTP, to its own port.
    assert _upstreams(rendered) == {
        f"http://origin.example.com:{port}" for port in runtime["listeners"]["http"]
    }
    for block in http_blocks:
        assert "proxy_pass" in block


def test_under_attack_mode_redirects_permanently_on_every_challenge_path():
    """The mitigation endpoints redirect with the same code as the proxy path.

    A site in Under Attack Mode with Always Use HTTPS has three HTTP-side
    redirects — the two challenge endpoints and the named location the guarded
    request falls through to — and one of them keeping 308 would answer some
    visitors differently from the rest.
    """
    site = _mode_site(
        SslMode.FULL,
        serves_tls=True,
        always_use_https=True,
        under_attack_mode=True,
    )
    rendered = _render(site_to_ansible(site), blitzecdn_nginx_under_attack_enabled=True)
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    http_blocks, _ = _server_blocks(rendered, defaults)

    assert "return 308" not in rendered
    for block in http_blocks:
        assert "proxy_pass" not in block
        # challenge, verify, and the guarded fall-through, per HTTP listener.
        assert block.count("return 301 https://$host$request_uri;") == 3
    assert _upstreams(rendered) == {
        f"https://origin.example.com:{port}" for port in runtime["listeners"]["https"]
    }


def test_always_use_https_can_be_disabled_without_disabling_tls():
    site = CdnSite.model_validate(
        {
            "name": "both-schemes",
            "server_names": ["both.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "always_use_https": False,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    rendered = _render(site_to_ansible(site))

    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2
    assert "listen 443 ssl;" in rendered


def test_websocket_upgrade_is_forwarded_and_never_cached():
    site = CdnSite.model_validate(
        {
            "name": "socket",
            "server_names": ["socket.example.com"],
            "origin_host": "origin.example.com",
        }
    )
    rendered = _render(site_to_ansible(site))
    http_template = (ROLE_DIR / "templates/http.conf.j2").read_text(encoding="utf-8")

    assert "map $http_upgrade $blitzecdn_connection_upgrade" in http_template
    assert "proxy_set_header Upgrade $http_upgrade;" in rendered
    assert "proxy_set_header Connection $blitzecdn_connection_upgrade;" in rendered
    assert "proxy_cache_bypass $http_upgrade;" in rendered
    assert "proxy_no_cache $http_upgrade;" in rendered


@pytest.mark.parametrize(
    ("minimum", "protocols"),
    [
        (MinimumTlsVersion.TLS_1_2, "TLSv1.2 TLSv1.3"),
        (MinimumTlsVersion.TLS_1_3, "TLSv1.3"),
    ],
)
def test_minimum_tls_version_renders_per_hostname(minimum, protocols):
    site = CdnSite.model_validate(
        {
            "name": "tls-minimum",
            "server_names": ["tls.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "minimum_tls_version": minimum,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    assert f"ssl_protocols {protocols};" in _render(site_to_ansible(site))


@pytest.mark.parametrize(
    ("mode", "key_uri"),
    [
        (CacheQueryStringMode.INCLUDE, "$request_uri"),
        (CacheQueryStringMode.IGNORE, "$blitzecdn_uri_without_query"),
    ],
)
def test_cache_query_string_mode_selects_the_cache_key(mode, key_uri):
    site = CdnSite.model_validate(
        {
            "name": "query-mode",
            "server_names": ["query.example.com"],
            "origin_host": "origin.example.com",
            "cache_query_string_mode": mode,
        }
    )

    rendered = _render(site_to_ansible(site))

    assert (
        f'proxy_cache_key "$scheme$server_port$request_method$host{key_uri}' in rendered
    )
    # Ignore affects identity in the cache, not what the origin receives.
    assert "proxy_pass $blitzecdn_upstream$request_uri;" in rendered


def _compressed(mode: CompressionMode) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "compressed",
                "server_names": ["compressed.example.com"],
                "origin_host": "origin.example.com",
                "compression": mode,
            }
        )
    )


def test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback():
    site = _compressed(CompressionMode.BROTLI)
    rendered = _render(site)
    assert "brotli on;" in rendered
    assert "brotli_comp_level 5;" in rendered
    assert "gzip on;" in rendered, "Brotli never replaces the gzip fallback"


def test_compression_off_says_so_rather_than_staying_silent():
    """Debian's nginx.conf carries `gzip on` in the http context.

    Omitting the directive therefore does not mean off — it means inherited.
    """
    rendered = _render(_compressed(CompressionMode.OFF))

    assert "gzip off;" in rendered
    assert "gzip on;" not in rendered


def test_gzip_only_turns_the_managed_brotli_filter_off():
    rendered = _render(_compressed(CompressionMode.GZIP))

    assert "gzip on;" in rendered
    assert "brotli off;" in rendered
    assert "brotli on;" not in rendered


def test_compression_never_lists_text_html():
    """Both modules always compress it, and gzip warns about the duplicate.

    A warning on every `nginx -t` is how operators learn to read a noisy config
    test as normal, which is the failure this prevents rather than the
    duplicate itself.
    """
    compression_role = (
        REPO_ROOT
        / "packages/blitzecdn-compression/src/blitzecdn_compression/ansible/roles"
        / "blitzecdn_compression"
    )
    types = _defaults_of(compression_role)["blitzecdn_compression_types"]

    assert "text/html" not in types
    # Nothing already compressed: re-encoding one of these adds bytes and CPU.
    assert not {"image/jpeg", "image/png", "font/woff2", "application/zip"} & set(types)


def test_compression_leaves_the_cache_key_and_origin_request_alone():
    """The edge compresses on egress; the cache still stores what the origin sent.

    If this ever stopped holding, a cache entry would carry an encoding that
    the key does not distinguish, and a client would be handed a body it cannot
    decode — the exact failure the Accept-Encoding map exists to prevent.
    """
    rendered = _render(_compressed(CompressionMode.BROTLI))

    assert "proxy_set_header Accept-Encoding $blitzecdn_accept_encoding;" in rendered
    assert (
        'proxy_cache_key "$scheme$server_port$request_method$host$request_uri'
        '$blitzecdn_accept_encoding"' in rendered
    )
    # gzip_vary is for shared caches downstream of us, which do not know our key.
    assert "gzip_vary on;" in rendered
    # Proxied responses carry headers that disable gzip under the default.
    assert "gzip_proxied any;" in rendered


def test_managed_nginx_stack_uses_ubuntu_abi_matched_modules():
    """The stack is one ABI unit, now as an image rather than an apt transaction.

    Which makes the unit stronger rather than weaker: the binary and its three
    dynamic modules are resolved together at build time, published together,
    and pulled together as one immutable object. Nothing on an edge can pair
    one version's binary with another version's modules, because nothing on an
    edge installs either. tests/test_ansible_role_contracts.py holds the
    Dockerfile end of this; here the point is that the *edge* no longer names
    the packages at all.
    """
    assert "blitzecdn_nginx_packages" not in _role_defaults()
    assert (
        "ghcr.io/misaf/blitzecdn-edge"
        in _runtime_defaults()["blitzecdn_edge_runtime_image_default"]
    )


def _under_attack_site(*, enabled: bool = True) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "mitigated",
                "server_names": ["mitigated.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
                "under_attack_mode": enabled,
                "always_use_https": True,
            }
        )
    )


def test_under_attack_mode_is_absent_from_the_disabled_request_flow():
    rendered = _render(_under_attack_site(enabled=False))

    assert "blitzecdn_under_attack.guard" not in rendered
    assert "X-BlitzeCDN-Mitigation challenge" not in rendered
    assert "auth_request /.blitzecdn/" not in rendered
    assert "proxy_pass" in rendered


def test_under_attack_mode_renders_before_redirect_and_proxy_on_http_and_https():
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)

    assert rendered.count("auth_request /.blitzecdn/internal/under-attack-guard;") == (
        len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])
    )
    assert "try_files /__blitzecdn_dispatch__ @blitzecdn_upstream;" in rendered
    assert "proxy_pass" in rendered
    assert rendered.index("auth_request /.blitzecdn/") < rendered.index("proxy_pass")
    assert "X-BlitzeCDN-Mitigation challenge" in rendered


def test_under_attack_reserved_endpoints_are_edge_only_and_uncached():
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)

    assert "location = /.blitzecdn/challenge {" in rendered
    assert "location = /.blitzecdn/challenge/verify {" in rendered
    assert "location ^~ /.blitzecdn/" in rendered
    assert "js_content blitzecdn_under_attack.challenge;" in rendered
    assert "js_content blitzecdn_under_attack.verify;" in rendered
    assert 'Cache-Control "no-store' in rendered
    assert "limit_req zone=blitzecdn_under_attack_verify" in rendered

    for marker in (
        "location = /.blitzecdn/challenge {",
        "location = /.blitzecdn/challenge/verify {",
    ):
        block = rendered.split(marker, 1)[1].split("    }", 1)[0]
        assert "proxy_pass" not in block
        assert "proxy_cache" not in block


def test_acme_bypasses_under_attack_mode_in_every_server_block():
    defaults = _role_defaults()
    runtime = defaults["blitzecdn_edge_runtime"]
    rendered = _render(_under_attack_site(), blitzecdn_nginx_under_attack_enabled=True)
    expected = len(runtime["listeners"]["http"]) + len(runtime["listeners"]["https"])

    assert rendered.count("location ^~ /.well-known/acme-challenge/ {") == expected
    acme_blocks = rendered.split("location ^~ /.well-known/acme-challenge/ {")[1:]
    for block in acme_blocks:
        location = block.split("    }", 1)[0]
        assert "auth_request" not in location
        assert "js_content" not in location
        assert "allow " not in location
        assert "deny " not in location


def test_missing_compression_preserves_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role.

    Unlike always_use_https, the safe default here is on: the role's default
    and the domain's agree on brotli, so an edge upgraded ahead of its
    controller compresses exactly as it will once both have moved.
    """
    site = _compressed(CompressionMode.BROTLI)
    del site["compression"]

    option = _role_spec()["blitzecdn_nginx_sites"]["options"]["compression"]
    assert option["default"] == "brotli"
    assert option.get("required", False) is False

    assert "gzip on;" in _render(site)


def test_missing_always_use_https_preserves_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role."""
    site = site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "pre-upgrade",
                "server_names": ["pre-upgrade.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
            }
        )
    )
    del site["always_use_https"]

    option = _role_spec()["blitzecdn_nginx_sites"]["options"]["always_use_https"]
    assert option["default"] is False
    assert option.get("required", False) is False

    rendered = _render(site)
    assert "return 301 https://$host$request_uri;" not in rendered
    assert rendered.count("proxy_pass") >= 2


def test_committed_fixture_matches_generated_desired_state(desired_state):
    """CI feeds this fixture to a real playbook; keep it honest.

    Set BLITZECDN_UPDATE_FIXTURE=1 to rewrite it after an intentional change.
    """
    if os.environ.get("BLITZECDN_UPDATE_FIXTURE"):
        FIXTURE.write_text(
            "---\n" + yaml.dump(desired_state, Dumper=_IndentedDumper, sort_keys=False),
            encoding="utf-8",
        )
    committed = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert committed == desired_state, (
        f"{FIXTURE} is stale. Regenerate it with:\n"
        "  BLITZECDN_UPDATE_FIXTURE=1 .venv/bin/python -m pytest "
        "tests/test_contract.py"
    )
