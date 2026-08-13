"""Verify what the control plane emits against what the edge roles declare.

The edge roles live in `ansible/roles/`, in this repository, so these tests read
the roles this control plane actually deploys. Nothing else stops a new
`CdnSite` field from reaching a role that has never heard of it.

Every assertion here is about the boundary between them, not either side alone:

* every key `CdnSite.to_ansible()` emits is declared in `argument_specs.yml`;
* every key the role marks required is actually present;
* declared `choices` cover the values the domain enums can produce;
* `site.conf.j2` renders from real model output without raising.

When one of these fails, change the model and the role together in one commit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from blitzecdn.control_plane import ControlPlane
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.sites import (
    CdnSite,
    CertificateMode,
    HttpScheme,
    SiteFirewall,
)
from blitzecdn.infrastructure.database import Repository

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures/desired-state.yml"

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


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _role_defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8"))


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    """Render desired state the way a real deployment would.

    From records, because that is the only way a site comes to exist. The
    snapshot this renders from carries records and derives the sites on read,
    so writing to the derived table here would describe a state no deployment
    can actually produce.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings, repository)
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
                "origin_port": 8443,
                "origin_scheme": HttpScheme.HTTPS,
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
                "origin_scheme": HttpScheme.HTTP,
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


def test_every_role_a_playbook_names_exists():
    """The failure the collection pin used to catch, in its remaining form.

    With the roles pinned by version, a playbook could name a role the pinned
    release did not have. They ship together now, so the only way to get that
    wrong is to rename or delete a role and miss a reference — which Ansible
    would report at deploy time, on a real edge, halfway through a run.
    """
    referenced: set[str] = set()
    for playbook in sorted((PROJECT_DIR / "ansible/playbooks").glob("*.yml")):
        document = yaml.safe_load(playbook.read_text(encoding="utf-8"))
        for play in document:
            for entry in play.get("roles", []):
                name = entry["role"] if isinstance(entry, dict) else entry
                referenced.add(name)
                assert (ROLES_DIR / name).is_dir(), (
                    f"{playbook.name} names role {name}, which is not in ansible/roles/"
                )

    assert "blitzecdn_nginx" in referenced, "the sweep found no playbooks to check"


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
            f"CdnSite emits {sorted(undeclared)}, which the pinned "
            "blitzecdn.edge collection does not declare in "
            "roles/blitzecdn_nginx/meta/argument_specs.yml. Add them in the "
            "edge repository and release it."
        )


def test_required_keys_are_always_emitted(desired_state):
    options = _role_spec()["blitzecdn_nginx_sites"]["options"]
    required = {name for name, spec in options.items() if (spec or {}).get("required")}
    for site in desired_state["blitzecdn_nginx_sites"]:
        missing = required - set(site)
        assert not missing, (
            f"site {site.get('name')!r} omits required {sorted(missing)}"
        )


@pytest.mark.parametrize(
    ("field", "enum"),
    [("origin_scheme", HttpScheme), ("certificate_mode", CertificateMode)],
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


def test_geoip_is_off_until_an_operator_turns_it_on():
    """Country rules depend on a database this collection cannot install.

    Defaulting the switch on would render every edge unable to load nginx the
    moment someone wrote a country rule, so the role asserts instead. That is
    only true while the default stays false.
    """
    assert _role_defaults()["blitzecdn_nginx_geoip_enabled"] is False


def _render(site: dict[str, Any], **overrides: Any) -> str:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    return environment.get_template("site.conf.j2").render(
        **(_role_defaults() | overrides), item=site
    )


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
    site = CdnSite.model_validate(
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
    ).to_ansible()
    rendered = _render(site, blitzecdn_nginx_geoip_enabled=True)
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
    site = CdnSite.model_validate(
        {
            "name": "geo",
            "server_names": ["geo.example.com"],
            "origin_host": "origin.example.com",
            "firewall": {"allowed_countries": ["DE", "FR"]},
        }
    ).to_ansible()
    rendered = _render(site, blitzecdn_nginx_geoip_enabled=True)
    assert 'if ($blitzecdn_country !~ "^(DE|FR)$")' in rendered


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
            "origin_scheme": HttpScheme.HTTPS,
        }
    )
    assert site.effective_origin_sni == "origin.example.com"
    rendered = _render(site.to_ansible())
    assert "proxy_ssl_name origin.example.com;" in rendered


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


# ----------------------------------------------------------------------
# Cross-role agreements
#
# blitzecdn_cache and blitzecdn_stats each recompute or re-read something
# blitzecdn_nginx configured. Nothing at run time can tell a disagreement from
# an ordinary empty result: a purge aimed at the wrong directory reports
# success having deleted nothing, and a reader aimed at the wrong log reports a
# hit ratio of zero, which looks like a broken cache rather than a broken
# reader. These are the only guards.
# ----------------------------------------------------------------------

CACHE_ROLE_DIR = _role("blitzecdn_cache")
STATS_ROLE_DIR = _role("blitzecdn_stats")


def _defaults_of(role_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("cache_key", "nginx_key"),
    [
        ("blitzecdn_cache_path", "blitzecdn_nginx_cache_path"),
        (
            "blitzecdn_cache_normalize_accept_encoding",
            "blitzecdn_nginx_normalize_accept_encoding",
        ),
    ],
)
def test_purge_role_agrees_with_the_nginx_role(cache_key, nginx_key):
    """A purge computes file paths from these; a mismatch purges nothing."""
    assert _defaults_of(CACHE_ROLE_DIR)[cache_key] == _role_defaults()[nginx_key], (
        f"{cache_key} in blitzecdn_cache disagrees with {nginx_key} in "
        "blitzecdn_nginx. Purge would delete paths nginx never wrote to and "
        "report success."
    )


def test_stats_role_reads_the_log_the_nginx_role_writes():
    stats = _defaults_of(STATS_ROLE_DIR)
    nginx = _role_defaults()
    assert (
        stats["blitzecdn_stats_access_log_path"]
        == nginx["blitzecdn_nginx_access_log_path"]
    )
    assert (
        stats["blitzecdn_stats_status_address"]
        == nginx["blitzecdn_nginx_status_address"]
    )
    assert stats["blitzecdn_stats_status_port"] == nginx["blitzecdn_nginx_status_port"]
    assert stats["blitzecdn_stats_status_path"] == nginx["blitzecdn_nginx_status_path"]


def test_purge_role_only_claims_the_cache_layout_the_nginx_role_emits():
    """The role computes <md5[-1]>/<md5[-3:-1]>/<md5>, which is levels=1:2 only.

    If the nginx template ever emits different levels, the purge role must stop
    claiming it can compute paths rather than silently deleting wrong ones.
    """
    template = (ROLE_DIR / "templates/cache.conf.j2").read_text(encoding="utf-8")
    assert "levels=1:2" in template
    spec = yaml.safe_load(
        (CACHE_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]
    assert spec["blitzecdn_cache_levels"]["choices"] == ["1:2"]


def test_purge_covers_every_cache_key_variant_the_site_template_can_produce():
    """One URL is several entries, and leaving one behind still serves it.

    The site template puts the request method and the normalized encoding in
    the key, so the purge role has to sweep both dimensions. nginx caches GET
    and HEAD by default and the template does not narrow proxy_cache_methods.
    """
    site_template = (ROLE_DIR / "templates/site.conf.j2").read_text(encoding="utf-8")
    assert "$scheme$request_method$host$request_uri" in site_template
    assert "proxy_cache_methods" not in site_template

    defaults = _defaults_of(CACHE_ROLE_DIR)
    assert set(defaults["blitzecdn_cache_purge_methods"]) == {"GET", "HEAD"}

    cache_conf = (ROLE_DIR / "templates/cache.conf.j2").read_text(encoding="utf-8")
    mapped = set(re.findall(r'"(br|gzip)"\s*;', cache_conf)) | {""}
    assert set(defaults["blitzecdn_cache_purge_encodings"]) == mapped, (
        "the encodings blitzecdn_cache purges differ from the ones the "
        "$blitzecdn_accept_encoding map can produce; the unlisted variant "
        "would survive a purge and keep being served"
    )


# ----------------------------------------------------------------------
# Executing the role's validation tasks
#
# Everything above reads the role as data or renders its templates. Neither
# evaluates a `when:` or an `assert`, and `--syntax-check` and ansible-lint do
# not either — a conditional that raises at run time passes all of them. That
# gap shipped a broken deploy once: `when: item.firewall is defined and
# item.firewall` parses, lints, and then fails on ansible-core 2.19+ because
# its result is a dict rather than a boolean.
#
# roles/blitzecdn_nginx/tasks/validate.yml holds every task that inspects
# desired state and refuses to proceed, and changes nothing on the host, so it
# can be run here against real model output.
# ----------------------------------------------------------------------

VALIDATE_TASKS = ROLE_DIR / "tasks/validate.yml"


def _run_validation(sites: list[dict[str, Any]], tmp_path: Path, **overrides: Any):
    """Execute the role's validation tasks against localhost."""
    ansible = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(ansible).exists():
        pytest.skip("ansible-playbook is not installed")
    variables = (
        _role_defaults()
        | {
            "blitzecdn_nginx_sites": sites,
        }
        | overrides
    )
    playbook = tmp_path / "validate.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(VALIDATE_TASKS)}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 - fixed argv built in this test
        [ansible, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        # pytest-cov's subprocess hook would otherwise make the Ansible
        # child write statement-only coverage beside this run's branch data,
        # which coverage refuses to combine.
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("COV_CORE", "COVERAGE"))
        }
        | {"ANSIBLE_LOCALHOST_WARNING": "False"},
        check=False,
    )


def test_role_validation_tasks_actually_run(desired_state, tmp_path):
    """The regression guard for a conditional that only fails at run time."""
    result = _run_validation(desired_state["blitzecdn_nginx_sites"], tmp_path)
    assert result.returncode == 0, (
        "the role's validation tasks failed against real desired state:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_role_refuses_country_rules_without_geoip(desired_state, tmp_path):
    """The deploy must stop, and say which variable turns the feature on."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"firewall": {"denied_countries": ["RU"]}}

    result = _run_validation(sites, tmp_path, blitzecdn_nginx_geoip_enabled=False)

    assert result.returncode != 0
    assert "blitzecdn_nginx_geoip_enabled" in result.stdout

    allowed = _run_validation(sites, tmp_path, blitzecdn_nginx_geoip_enabled=True)
    assert allowed.returncode == 0, allowed.stdout


def test_role_rejects_a_firewall_rule_it_cannot_safely_render(desired_state, tmp_path):
    """Defence in depth: the role holds even if the control plane regresses."""
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"firewall": {"denied_paths": ["/a; return 200"]}}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "firewall rules this role will not render" in result.stdout


def test_role_rejects_a_wildcard_on_an_ip_address(desired_state, tmp_path):
    """Both halves of this control have to refuse it, not just the controller.

    `*.192.0.2.1` satisfies the hostname shape check — every label of an IPv4
    literal is a valid DNS label — and nginx renders `server_name *.192.0.2.1`
    without complaint, then matches no request ever sent. The control plane
    refuses it now, and this is the redundancy: the value reaches a directive
    the role writes as root.
    """
    sites = [dict(site) for site in desired_state["blitzecdn_nginx_sites"]]
    sites[0] = sites[0] | {"server_names": ["*.192.0.2.1"]}

    result = _run_validation(sites, tmp_path)

    assert result.returncode != 0
    assert "Invalid CDN site" in result.stdout


def test_maxmind_credentials_never_reach_an_nginx_config():
    """The license key belongs in one 0600 file and nowhere else.

    /etc/nginx/conf.d is world-readable, and the geoip2 directive needs only
    the database path. A key that leaked into a template here would be
    disclosed to every account on the edge.
    """
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["dirname"] = os.path.dirname
    context = _role_defaults() | {
        "blitzecdn_nginx_geoip_enabled": True,
        "blitzecdn_nginx_geoip_account_id": "123456",
        "blitzecdn_nginx_geoip_license_key": "SENTINELKEY",
    }
    nginx_side = environment.get_template("geoip.conf.j2").render(**context)
    assert "SENTINELKEY" not in nginx_side
    assert "123456" not in nginx_side
    assert context["blitzecdn_nginx_geoip_database"] in nginx_side

    # …and it does reach the file that is supposed to carry it.
    credentials = environment.get_template("maxmind.conf.j2").render(**context)
    assert "LicenseKey SENTINELKEY" in credentials


def test_the_credentials_file_is_written_private_and_unlogged():
    """Mode and no_log are the whole protection; assert them, not the intent."""
    tasks = yaml.safe_load((ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8"))

    def walk(items):
        for task in items:
            yield task
            for key in ("block", "rescue", "always"):
                if key in task:
                    yield from walk(task[key])

    writer = next(
        task
        for task in walk(tasks)
        if task.get("ansible.builtin.template", {}).get("src") == "maxmind.conf.j2"
    )
    assert writer["ansible.builtin.template"]["mode"] == "0600"
    assert writer["ansible.builtin.template"]["owner"] == "root"
    assert writer["no_log"] is True


def test_the_stats_role_publishes_through_the_agreed_report_fact():
    """The channel a role returns a payload on is a contract like any other.

    `blitzecdn_stats` is the only role that returns data rather than an
    outcome, and the control plane finds it by looking for one fact name. If
    the role renamed it, every collection would come back empty with every edge
    reporting success — the silent-no-op failure this suite exists to catch.
    """
    tasks = (STATS_ROLE_DIR / "tasks/main.yml").read_text(encoding="utf-8")
    plugin = (PROJECT_DIR / "ansible/plugins/callback/blitzecdn_result.py").read_text(
        encoding="utf-8"
    )

    assert 'REPORT_FACT = "blitzecdn_report"' in plugin, (
        "the callback plugin no longer collects blitzecdn_report; the stats "
        "role publishes it and nothing else would carry the counters back"
    )
    assert "blitzecdn_report:" in tasks, (
        "blitzecdn_stats must publish its document as the blitzecdn_report "
        "fact — see REPORT_FACT in ansible/plugins/callback/blitzecdn_result.py"
    )


def test_the_stats_role_no_longer_wants_a_controller_directory():
    """Its report travels with the run, so there is no path to hand it.

    A resurrected `blitzecdn_stats_output_dir` would be a required option the
    control plane never sets, and role argument validation would fail every
    collection.
    """
    spec = yaml.safe_load(
        (STATS_ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )["argument_specs"]["main"]["options"]

    assert "blitzecdn_stats_output_dir" not in spec


def test_the_controller_environment_points_at_the_inventory_that_exists():
    """A settings file the controller reads must name a real inventory.

    `BLITZE_INVENTORY` outranks blitzecdn.toml, so a stale value here is not a
    cosmetic wrong default — it is the value the installed controller uses, and
    it made `blitzecdn doctor` report a configuration error on every fresh
    standalone install. It named `hosts.yml`, the static inventory that was
    deleted when the fleet moved into the database.

    Written on a first installation only, so a wrong value here also never
    corrects itself on a later converge.
    """
    template = (
        Path(__file__).parents[1]
        / "ansible/roles/blitzecdn_controlplane/templates/blitzecdn.env.j2"
    ).read_text(encoding="utf-8")
    assert "inventory/blitzecdn.yml" in template
    assert "hosts.yml" not in template


def test_standalone_environment_uses_public_dns_for_certificate_preflight():
    """NAT-local DNS must not make public certificate checks compare fiction."""
    template = (
        Path(__file__).parents[1]
        / "ansible/roles/blitzecdn_controlplane/templates/blitzecdn.env.j2"
    ).read_text(encoding="utf-8")
    assert (
        "BLITZE_PREFLIGHT_DNS_SERVERS="
        "{{ blitzecdn_controlplane_preflight_dns_servers }}" in template
    )

    defaults = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / "ansible/roles/blitzecdn_controlplane/defaults/main.yml"
        ).read_text(encoding="utf-8")
    )
    assert defaults["blitzecdn_controlplane_preflight_dns_servers"] == (
        "1.1.1.1,1.0.0.1"
    )
