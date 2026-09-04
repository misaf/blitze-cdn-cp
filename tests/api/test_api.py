import time

from control_plane_fixtures import (
    API_HEADERS,
    control_plane_app,
    host_run,
    seed_site_over_http,
)
from fastapi.testclient import TestClient

from blitzecdn.api.models import SitePatch
from blitzecdn.api.models import SitePolicy as SitePolicyModel
from blitzecdn.capabilities.deployments import DeploymentService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.sites.domain import SitePolicy
from blitzecdn.core.database import Repository
from blitzecdn.core.exceptions import (
    ConfigurationError,
    DeploymentBusyError,
    ExecutionError,
)
from blitzecdn.core.operations import WorkflowKind


def test_the_api_carries_every_policy_field_the_domain_has():
    """A knob an operator can set but never see would fail nowhere else.

    There is one published version, so there is no frozen representation to
    project a new field away from: a field added to `SitePolicy` is expected
    here and in the patch body, and this is what says so.
    """
    missing = set(SitePolicy.model_fields) - set(SitePolicyModel.model_fields)
    assert not missing, f"the API does not expose {sorted(missing)}"

    unpatchable = set(SitePolicy.model_fields) - set(SitePatch.model_fields)
    assert not unpatchable, f"the API cannot PATCH {sorted(unpatchable)}"


def test_interrupted_workflows_are_recovered_and_visible(settings):
    repository = Repository(settings.database_path)
    workflow = repository.workflows.create(
        "interrupted", WorkflowKind.CERTIFICATE, "alice", "cdn-example-com"
    )

    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v1/workflows").status_code == 401
        response = client.get("/v1/workflows", headers={"X-API-Key": "x" * 32})
        detail = client.get(
            f"/v1/workflows/{workflow.id}", headers={"X-API-Key": "x" * 32}
        )

    assert response.status_code == 200
    assert response.json()[0]["status"] == "needs_review"
    assert detail.json()["error"].startswith("the controller restarted")


def test_domain_and_record_crud_and_errors(settings, domain_payload, record_payload):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post("/v1/domains", json=domain_payload, headers=headers).status_code
            == 201
        )
        assert (
            client.post("/v1/domains", json=domain_payload, headers=headers).status_code
            == 409
        )
        # A record cannot exist without its zone.
        orphan = client.post(
            "/v1/domains/absent.example/records",
            json={**record_payload, "domain": "absent.example"},
            headers=headers,
        )
        assert orphan.status_code == 404

        # A record cannot route to a site that does not exist either.
        assert (
            client.post(
                "/v1/domains/example.com/records", json=record_payload, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/v1/sites",
                json={"name": "cdn-example-com", "origin_host": "198.51.100.10"},
                headers=headers,
            ).status_code
            == 201
        )
        created = client.post(
            "/v1/domains/example.com/records", json=record_payload, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["site"] == "cdn-example-com"

        # The body's domain must agree with the path.
        mismatched = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "domain": "other.example", "name": "x"},
            headers=headers,
        )
        assert mismatched.status_code == 409

        # Routing a record is what puts a hostname on the virtual host.
        sites = client.get("/v1/sites", headers=headers).json()
        assert len(sites) == 1
        assert sites[0]["server_names"] == ["cdn.example.com"]
        assert sites[0]["always_use_https"] is False
        assert sites[0]["minimum_tls_version"] == "1.2"
        assert sites[0]["cache_query_string_mode"] == "include"

        redirect = client.patch(
            "/v1/sites/cdn-example-com",
            json={"always_use_https": True},
            headers=headers,
        )
        assert redirect.status_code == 200
        assert redirect.json()["always_use_https"] is True

        policy = client.patch(
            "/v1/sites/cdn-example-com",
            json={
                "minimum_tls_version": "1.3",
                "cache_query_string_mode": "ignore",
            },
            headers=headers,
        )
        assert policy.status_code == 200
        assert policy.json()["minimum_tls_version"] == "1.3"
        assert policy.json()["cache_query_string_mode"] == "ignore"

        # Unrouting takes the hostname off the site and leaves the site.
        unrouted = client.patch(
            "/v1/domains/example.com/records/cdn",
            json={"site": None, "value": "203.0.113.7"},
            headers=headers,
        )
        assert unrouted.json()["site"] is None
        assert unrouted.json()["value"] == "203.0.113.7"
        assert client.get("/v1/sites", headers=headers).json()[0]["server_names"] == []

        # And a site nothing routes to can be deleted; one with hostnames cannot.
        assert (
            client.delete("/v1/sites/cdn-example-com", headers=headers).status_code
            == 204
        )
        assert (
            client.delete(
                "/v1/domains/example.com/records/cdn", headers=headers
            ).status_code
            == 204
        )
        assert (
            client.delete("/v1/domains/example.com", headers=headers).status_code == 204
        )
        assert client.get("/v1/deployments/missing", headers=headers).status_code == 404


def test_a_site_is_created_and_deleted_but_its_hostnames_are_not_writable(settings):
    """The write routes exist now; `server_names` still is not a field a client sets.

    It is the set of records routed to the site, so a body carrying it is
    refused rather than quietly ignored — the alternative is a client that
    believes it set the hostnames and a site that never served them.
    """
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post(
                "/v1/sites",
                json={
                    "name": "cdn-example-com",
                    "origin_host": "198.51.100.10",
                    "server_names": ["cdn.example.com"],
                },
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/sites",
                json={"name": "cdn-example-com", "origin_host": "198.51.100.10"},
                headers=headers,
            ).json()["server_names"]
            == []
        )
        schema = client.get("/openapi.json").json()
        assert set(schema["paths"]["/v1/sites"]) == {"get", "post"}
        assert set(schema["paths"]["/v1/sites/{name}"]) == {"get", "patch", "delete"}


def test_dns_export_omits_addresses_for_proxied_records(
    settings, domain_payload, record_payload
):
    """A routed name must resolve to an edge, and edge IPs are not ours."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        client.post(
            "/v1/domains/example.com/records",
            json={"domain": "example.com", "name": "db", "value": "198.51.100.10"},
            headers=headers,
        )
        exported = {
            row["fqdn"]: row
            for row in client.get("/v1/dns/export", headers=headers).json()
        }
        assert "value" not in exported["cdn.example.com"]
        assert exported["cdn.example.com"]["site"] == "cdn-example-com"
        assert exported["db.example.com"]["value"] == "198.51.100.10"


def test_deploy_returns_202_immediately_and_stays_durable_until_a_worker_runs(
    settings, site_payload
):
    """A convergence can outlast any HTTP client, so the request must not block."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        queued = client.post("/v1/deployments", json={"check": True}, headers=headers)
        assert queued.status_code == 202
        deployment_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"

        body = client.get(f"/v1/deployments/{deployment_id}", headers=headers).json()
        assert body["status"] == "queued"


def test_a_deploy_can_be_narrowed_to_some_edges(settings):
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post(
            "/v1/deployments",
            json={"check": True, "host_limit": "edge-a"},
            headers=API_HEADERS,
        )
        assert accepted.status_code == 202
        assert accepted.json()["host_limit"] == "edge-a"


def test_a_limit_that_could_widen_a_deploy_is_a_422(settings):
    """Rejected at the schema, so no deployment row is created to explain."""
    with TestClient(control_plane_app(settings)) as client:
        for pattern in ("edge-a:database-1", "edge-a:!edge-b", "@/etc/passwd"):
            response = client.post(
                "/v1/deployments",
                json={"host_limit": pattern},
                headers=API_HEADERS,
            )
            assert response.status_code == 422, pattern
        assert client.get("/v1/deployments", headers=API_HEADERS).json() == []


def test_drift_queues_a_check_run_and_reads_back_as_a_report(settings):
    with TestClient(control_plane_app(settings)) as client:
        queued = client.post("/v1/drift", json={}, headers=API_HEADERS)
        assert queued.status_code == 202
        assert queued.json()["check_mode"] is True

        deployment_id = queued.json()["id"]
        for _ in range(50):
            report = client.get(
                f"/v1/deployments/{deployment_id}/drift", headers=API_HEADERS
            )
            if report.status_code == 200:
                break
            time.sleep(0.05)
        assert report.status_code == 200
        assert report.json()["deployment_id"] == deployment_id


def test_an_applied_deployment_is_not_readable_as_drift(settings):
    with TestClient(control_plane_app(settings)) as client:
        queued = client.post(
            "/v1/deployments", json={"check": False}, headers=API_HEADERS
        )
        deployment_id = queued.json()["id"]
        response = client.get(
            f"/v1/deployments/{deployment_id}/drift", headers=API_HEADERS
        )
        assert response.status_code == 409


def test_a_single_site_is_readable_by_name(settings):
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, API_HEADERS)
        name = client.get("/v1/sites", headers=API_HEADERS).json()[0]["name"]

        response = client.get(f"/v1/sites/{name}", headers=API_HEADERS)

        assert response.status_code == 200
        assert response.json()["name"] == name
        # The defaults the create body never mentioned.
        assert response.json()["ssl_mode"] == "off"
        assert response.json()["ssl_automatic_mode"] == "auto"
        assert "origin_scheme" not in response.json()


def test_removed_origin_scheme_is_rejected_on_create(
    settings, domain_payload, record_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=API_HEADERS)
        response = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "origin_scheme": "https"},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_scheme_is_rejected_on_patch(
    settings, domain_payload, record_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=API_HEADERS)
        client.post(
            "/v1/domains/example.com/records",
            json=record_payload,
            headers=API_HEADERS,
        )
        response = client.patch(
            "/v1/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_scheme": "http"},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_create(
    settings, domain_payload, record_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=API_HEADERS)
        response = client.post(
            "/v1/domains/example.com/records",
            json={**record_payload, "origin_port": 8080},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_patch(
    settings, domain_payload, record_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/domains", json=domain_payload, headers=API_HEADERS)
        client.post(
            "/v1/domains/example.com/records",
            json=record_payload,
            headers=API_HEADERS,
        )
        response = client.patch(
            "/v1/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_port": 8080},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_an_unknown_site_is_a_404(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v1/sites/absent", headers=API_HEADERS).status_code == 404


# ----------------------------------------------------------------------
# Error mapping. A permanent condition must not be reported as a transient
# one: a client that retries a ConfigurationError retries forever, and an
# alerting rule watching for 503 pages someone about a controller that is
# working correctly and declining bad input.
# ----------------------------------------------------------------------


# Raised from a *core* operation. These used to monkeypatch `check_origins`,
# which now lives in `blitzecdn-origins`: the mapping being asserted is the
# control plane's exception handlers, and a test of core's handlers that only
# runs while an optional package is installed is testing the wrong thing.


def test_a_configuration_error_is_a_400_rather_than_a_503(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda _self, _name, _operator, **_kwargs: (_ for _ in ()).throw(
            ConfigurationError("edge does not exist: edge-01")
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        response = client.delete("/v1/edges/edge-01", headers=API_HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"] == "edge does not exist: edge-01"


def test_an_execution_error_is_a_502(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda _self, _name, _operator, **_kwargs: (_ for _ in ()).throw(
            ExecutionError("ansible would not start")
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        response = client.delete("/v1/edges/edge-01", headers=API_HEADERS)
    assert response.status_code == 502


def test_a_busy_deployment_is_a_409_that_says_when_to_come_back(settings, monkeypatch):
    """The one conflict that clears on its own, so the one that gets Retry-After."""

    def busy(_self, _operator, **_kwargs):
        raise DeploymentBusyError("another deployment is already running")

    monkeypatch.setattr(DeploymentService, "submit_deployment", busy)
    with TestClient(control_plane_app(settings)) as client:
        response = client.post("/v1/deployments", json={}, headers=API_HEADERS)
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "30"


# ----------------------------------------------------------------------
# Edges. Writing one here is what puts a host into the Ansible inventory:
# the `blitzecdn` plugin reads these same rows at the start of every run.
# ----------------------------------------------------------------------


_EDGE = {
    "name": "edge-01",
    "host": "192.0.2.10",
    "ssh_sources": ["198.51.100.0/24"],
}


def test_edge_crud_over_http(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v1/edges", headers=API_HEADERS).json() == []

        created = client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)
        assert created.status_code == 201
        assert created.json()["name"] == "edge-01"
        # Defaults are resolved by the model, not left for the plugin to guess.
        assert created.json()["user"] == "deploy"
        assert created.json()["port"] == 22

        assert (
            client.post("/v1/edges", json=_EDGE, headers=API_HEADERS).status_code == 409
        )
        assert len(client.get("/v1/edges", headers=API_HEADERS).json()) == 1
        assert (
            client.get("/v1/edges/edge-01", headers=API_HEADERS).json()["host"]
            == "192.0.2.10"
        )
        assert client.get("/v1/edges/absent", headers=API_HEADERS).status_code == 404

        patched = client.patch(
            "/v1/edges/edge-01",
            json={"port": 7845, "public_addresses": ["203.0.113.10"]},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["port"] == 7845
        assert patched.json()["public_addresses"] == ["203.0.113.10"]
        # Untouched fields survive a patch.
        assert patched.json()["ssh_sources"] == ["198.51.100.0/24"]

        assert (
            client.patch(
                "/v1/edges/absent", json={"port": 22}, headers=API_HEADERS
            ).status_code
            == 404
        )


def test_an_edge_patch_cannot_rename_the_host(settings):
    """The name is how certificates, audit history and `--limit` refer to it.

    `EdgePatch` has no name field and forbids extras, so a rename is a 422 at
    the boundary rather than a silently ignored key — which would leave the
    caller believing a rename had happened.
    """
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)

        response = client.patch(
            "/v1/edges/edge-01", json={"name": "edge-99"}, headers=API_HEADERS
        )

        assert response.status_code == 422


def test_an_invalid_management_cidr_is_a_422_not_a_stored_edge(settings):
    """Validation is the model's, so the API rejects exactly what the CLI does."""
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v1/edges",
            json={**_EDGE, "ssh_sources": ["anywhere"]},
            headers=API_HEADERS,
        )

        assert response.status_code == 422
        assert client.get("/v1/edges", headers=API_HEADERS).json() == []


def test_removing_an_edge_without_decommissioning_says_so(settings):
    """The narrow case, and the response has to be honest about it.

    `decommission=false` leaves BlitzeCDN's configuration and every TLS private
    key on the host. A 204 could not distinguish that from a real teardown, so
    the body reports which one happened.
    """
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)

        response = client.delete(
            "/v1/edges/edge-01?decommission=false", headers=API_HEADERS
        )

        assert response.status_code == 200
        assert response.json() == {
            "name": "edge-01",
            "decommissioned": False,
            "hosts": [],
        }
        assert client.get("/v1/edges", headers=API_HEADERS).json() == []


def test_a_failed_teardown_keeps_the_edge_registered(settings, monkeypatch):
    """Deregistering first would strand the host.

    The inventory is derived from these rows, so an edge removed before its
    teardown succeeded is a host no playbook can address again — still serving
    its last converged virtual hosts, still holding their private keys.
    """
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (_ for _ in ()).throw(
            ExecutionError("teardown of edge-01 failed")
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)

        response = client.delete("/v1/edges/edge-01", headers=API_HEADERS)

        assert response.status_code == 502
        assert [
            edge["name"] for edge in client.get("/v1/edges", headers=API_HEADERS).json()
        ] == ["edge-01"]


def test_registering_an_edge_is_audited(settings):
    """Unanswerable before: edges never went through a service, so never a bus."""
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)
        client.patch("/v1/edges/edge-01", json={"port": 7845}, headers=API_HEADERS)

        events = client.get("/v1/audit-events", headers=API_HEADERS).json()

        actions = [event["action"] for event in events]
        assert "edge.added" in actions
        assert "edge.updated" in actions
        update = next(event for event in events if event["action"] == "edge.updated")
        assert update["details"]["fields"] == ["port"]


def test_edge_routes_require_authentication(settings):
    """Every edge route is a control route; none may be read unauthenticated."""
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v1/edges").status_code == 401
        assert client.post("/v1/edges", json=_EDGE).status_code == 401
        assert client.patch("/v1/edges/edge-01", json={"port": 22}).status_code == 401
        assert client.delete("/v1/edges/edge-01").status_code == 401


def test_a_successful_teardown_reports_what_it_did(settings, monkeypatch):
    """The body carries the teardown result, which is why this is not a 204.

    During an incident "the edge is gone" is not the useful part — which tasks
    ran on which host is, and a forced removal that tore nothing down looks
    identical without it.
    """
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (
            host_run("edge-01", changes=("remove /etc/blitzecdn",)),
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v1/edges", json=_EDGE, headers=API_HEADERS)

        response = client.delete("/v1/edges/edge-01", headers=API_HEADERS)

        assert response.status_code == 200
        body = response.json()
        assert body["decommissioned"] is True
        assert [host["host"] for host in body["hosts"]] == ["edge-01"]
        assert body["hosts"][0]["changes"][0]["task"] == "remove /etc/blitzecdn"


def test_no_cloudflare_header_name_is_published_by_the_api(settings):
    """The BZ- namespace is the whole surface; CF- and True-Client-IP are not
    ours to define and must not appear as fields, defaults, or descriptions."""
    with TestClient(control_plane_app(settings)) as client:
        document = client.get("/openapi.json").text

    for foreign in ("CF-Connecting-IP", "cf_connecting_ip", "True-Client-IP"):
        assert foreign not in document


def test_http3_create_read_patch_and_validation(settings):
    site = {
        "name": "cdn-example-com",
        "origin_host": "198.51.100.10",
        "ssl_mode": "flexible",
        "http3_enabled": True,
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    with TestClient(control_plane_app(settings)) as client:
        created = client.post("/v1/sites", json=site, headers=API_HEADERS)
        assert created.status_code == 201
        assert created.json()["http3_enabled"] is True
        assert (
            client.get("/v1/sites", headers=API_HEADERS).json()[0]["http3_enabled"]
            is True
        )

        unchanged = client.patch(
            "/v1/sites/cdn-example-com",
            json={"http3_enabled": True},
            headers=API_HEADERS,
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["http3_enabled"] is True

        disabled = client.patch(
            "/v1/sites/cdn-example-com",
            json={"http3_enabled": False},
            headers=API_HEADERS,
        )
        assert disabled.status_code == 200
        assert disabled.json()["http3_enabled"] is False

        rejected = client.patch(
            "/v1/sites/cdn-example-com",
            json={"ssl_mode": "off", "http3_enabled": True},
            headers=API_HEADERS,
        )
        assert rejected.status_code == 422
        assert "requires ssl_mode" in rejected.text

        events = client.get("/v1/audit-events", headers=API_HEADERS).json()
        updates = [event for event in events if event["action"] == "site.updated"]
        assert any("http3_enabled" in event["details"]["fields"] for event in updates)


def test_under_attack_mode_is_visible_patchable_and_in_openapi(settings):
    with TestClient(control_plane_app(settings)) as client:
        schema = client.get("/openapi.json").json()
        property_schema = schema["components"]["schemas"]["SitePatch"]["properties"][
            "under_attack_mode"
        ]
        assert property_schema["anyOf"][0]["type"] == "boolean"

        created = client.post(
            "/v1/sites",
            json={"name": "cdn-example-com", "origin_host": "198.51.100.10"},
            headers=API_HEADERS,
        )
        assert created.status_code == 201
        assert created.json()["under_attack_mode"] is False

        patched = client.patch(
            "/v1/sites/cdn-example-com",
            json={"under_attack_mode": True},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["under_attack_mode"] is True
        assert (
            client.get("/v1/sites", headers=API_HEADERS).json()[0]["under_attack_mode"]
            is True
        )

        invalid = client.patch(
            "/v1/sites/cdn-example-com",
            json={"under_attack_mode": "sometimes"},
            headers=API_HEADERS,
        )
        assert invalid.status_code == 422


def test_compression_is_reported_patchable_and_validated(settings):
    payload = {"name": "cdn-example-com", "origin_host": "203.0.113.10"}
    with TestClient(control_plane_app(settings)) as client:
        created = client.post("/v1/sites", json=payload, headers=API_HEADERS)
        assert created.status_code == 201
        assert created.json()["compression"] == "brotli"

        patched = client.patch(
            "/v1/sites/cdn-example-com",
            json={"compression": "off"},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["compression"] == "off"

        rejected = client.patch(
            "/v1/sites/cdn-example-com",
            json={"compression": "deflate"},
            headers=API_HEADERS,
        )
        assert rejected.status_code == 422

        # A patch that does not name the field leaves it where it was.
        client.patch(
            "/v1/sites/cdn-example-com",
            json={"cache_enabled": False},
            headers=API_HEADERS,
        )
        assert (
            client.get("/v1/sites", headers=API_HEADERS).json()[0]["compression"]
            == "off"
        )


def test_visitor_headers_are_reported_replaced_wholesale_and_validated(settings):
    payload = {"name": "cdn-example-com", "origin_host": "203.0.113.10"}
    with TestClient(control_plane_app(settings)) as client:
        created = client.post("/v1/sites", json=payload, headers=API_HEADERS)
        assert created.status_code == 201
        assert created.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": False,
        }

        patched = client.patch(
            "/v1/sites/cdn-example-com",
            json={"visitor_headers": {"connecting_ip": False, "ip_country": True}},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["visitor_headers"] == {
            "connecting_ip": False,
            "ip_country": True,
        }

        # A partial block replaces the whole thing rather than merging, so the
        # unnamed switch comes back at its default.
        replaced = client.patch(
            "/v1/sites/cdn-example-com",
            json={"visitor_headers": {"ip_country": True}},
            headers=API_HEADERS,
        )
        assert replaced.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": True,
        }

        # No aliases, and no Cloudflare spelling smuggled in as an extra.
        for unknown in ({"cf_connecting_ip": True}, {"true_client_ip": True}):
            rejected = client.patch(
                "/v1/sites/cdn-example-com",
                json={"visitor_headers": unknown},
                headers=API_HEADERS,
            )
            assert rejected.status_code == 422
