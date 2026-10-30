import time

from fastapi.testclient import TestClient
from support.control_plane import (
    API_HEADERS,
    FakeRunner,
    build_application,
    control_plane_app,
    host_run,
    run_queued_operations,
    seed_site_over_http,
    serve,
)

from blitzecdn.capabilities.deployments import DeploymentService
from blitzecdn.capabilities.dns.api.models import CdnSite as CdnSiteModel
from blitzecdn.capabilities.dns.api.models import DomainPatch as DomainPatchModel
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import (
    ConfigurationError,
    DeploymentBusyError,
    ExecutionError,
)


def _zone(client, **policy):
    """A zone carrying the policy, and one proxied hostname so it derives a host.

    The record's value is the origin the edge fetches from.
    """
    created = client.post(
        "/v3/domains",
        json={"name": "example.com", **policy},
        headers=API_HEADERS,
    )
    client.post(
        "/v3/domains/example.com/records",
        json={
            "name": "cdn",
            "value": "198.51.100.10",
            "proxied": True,
        },
        headers=API_HEADERS,
    )
    return created


def test_the_api_carries_every_policy_field_the_domain_has():
    """A setting an operator can set but never see would fail nowhere else.

    A contributed setting is expected on a host and in the patch body.
    """
    published = set(CdnSiteModel.composed().model_fields)
    missing = set(SitePolicy.setting_fields()) - published
    assert not missing, f"the API does not expose {sorted(missing)}"

    patchable = set(DomainPatchModel.composed().model_fields)
    unpatchable = set(SitePolicy.setting_fields()) - patchable
    assert not unpatchable, f"the API cannot PATCH {sorted(unpatchable)}"


def test_interrupted_workflows_are_recovered_and_visible(settings):
    repository = Repository(settings.database_path)
    workflow = repository.workflows.create(
        "interrupted", "certificate", "alice", "example-com"
    )

    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/workflows").status_code == 401
        response = client.get("/v3/workflows", headers={"X-API-Key": "x" * 32})
        detail = client.get(
            f"/v3/workflows/{workflow.id}", headers={"X-API-Key": "x" * 32}
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "needs_review"
    assert detail.json()["error"].startswith("the controller restarted")


def test_domain_and_record_crud_and_errors(
    settings, domain_payload, record_create_payload
):
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        assert (
            client.post("/v3/domains", json=domain_payload, headers=headers).status_code
            == 201
        )
        assert (
            client.post("/v3/domains", json=domain_payload, headers=headers).status_code
            == 409
        )
        # A record cannot exist without its zone.
        orphan = client.post(
            "/v3/domains/absent.example/records",
            json=record_create_payload,
            headers=headers,
        )
        assert orphan.status_code == 404

        created = client.post(
            "/v3/domains/example.com/records",
            json=record_create_payload,
            headers=headers,
        )
        assert created.status_code == 201
        assert created.json()["proxied"] is True

        # The domain belongs in the URL, never the body.
        mismatched = client.post(
            "/v3/domains/example.com/records",
            json={**record_create_payload, "domain": "other.example", "name": "x"},
            headers=headers,
        )
        assert mismatched.status_code == 422

        # A proxied record is what puts a hostname on a virtual host.
        sites = client.get("/v3/hosts", headers=headers).json()["items"]
        assert len(sites) == 1
        assert sites[0]["server_names"] == ["cdn.example.com"]
        assert sites[0]["always_use_https"] is False
        assert sites[0]["minimum_tls_version"] == "1.2"
        assert sites[0]["cache_query_string_mode"] == "include"

        redirect = client.patch(
            "/v3/domains/example.com",
            json={"always_use_https": True},
            headers=headers,
        )
        assert redirect.status_code == 200
        assert redirect.json()["always_use_https"] is True
        assert (
            client.get("/v3/hosts", headers=headers).json()["items"][0][
                "always_use_https"
            ]
            is True
        )

        policy = client.patch(
            "/v3/domains/example.com",
            json={
                "minimum_tls_version": "1.3",
                "cache_query_string_mode": "ignore",
            },
            headers=headers,
        )
        assert policy.status_code == 200
        assert policy.json()["minimum_tls_version"] == "1.3"
        assert policy.json()["cache_query_string_mode"] == "ignore"

        # Unproxying takes the hostname off the edge and leaves the zone.
        unproxied = client.patch(
            "/v3/domains/example.com/records/cdn",
            json={"proxied": False, "value": "203.0.113.7"},
            headers=headers,
        )
        assert unproxied.json()["proxied"] is False
        assert unproxied.json()["value"] == "203.0.113.7"
        # With nothing proxied the zone derives no virtual host at all.
        assert client.get("/v3/hosts", headers=headers).json()["items"] == []
        assert (
            client.delete(
                "/v3/domains/example.com/records/cdn", headers=headers
            ).status_code
            == 204
        )
        assert (
            client.delete("/v3/domains/example.com", headers=headers).status_code == 204
        )
        assert client.get("/v3/deployments/missing", headers=headers).status_code == 404


def test_a_virtual_host_is_read_only_and_nothing_can_write_one(settings):
    """There is no writing counterpart, and the published document says so.

    A host is derived, so the collection publishes GET only; it changes through
    the zone, rule or records behind it.
    """
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        assert client.get("/v3/hosts", headers=headers).json()["items"][0][
            "server_names"
        ] == ["cdn.example.com"]
        for method in (client.post, client.put):
            assert (
                method(
                    "/v3/hosts",
                    json={"name": "example-com", "origin_host": "198.51.100.10"},
                    headers=headers,
                ).status_code
                == 405
            )
        assert (
            client.delete("/v3/hosts/example-com", headers=headers).status_code == 405
        )
        schema = client.get("/openapi.json").json()
        assert set(schema["paths"]["/v3/hosts"]) == {"get"}
        assert set(schema["paths"]["/v3/hosts/{name}"]) == {"get"}


def test_dns_export_omits_addresses_for_proxied_records(
    settings, domain_payload, record_create_payload
):
    """A routed name must resolve to an edge, and edge IPs are not ours."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        client.post(
            "/v3/domains/example.com/records",
            json={
                "name": "db",
                "proxied": False,
                "value": "198.51.100.10",
            },
            headers=headers,
        )
        document = client.get("/v3/dns/export", headers=headers).json()
        exported = {row["fqdn"]: row for row in document["records"]}
        assert "value" not in exported["cdn.example.com"]
        assert exported["cdn.example.com"]["proxied"] is True
        assert exported["cdn.example.com"]["answer"] == "an edge address"
        assert exported["db.example.com"]["value"] == "198.51.100.10"
        # And the client is told, in the same response, that nothing here is
        # going to publish any of it.
        assert document["publication"]["publishes"] is False
        assert "does not publish DNS" in document["publication"]["detail"]


def test_deploy_returns_202_immediately_and_stays_durable_until_a_worker_runs(
    settings, site_payload
):
    """A convergence can outlast any HTTP client, so the request must not block."""
    headers = {"X-API-Key": "x" * 32}
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, headers)
        queued = client.post("/v3/deployments", json={"check": True}, headers=headers)
        assert queued.status_code == 202
        deployment_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"

        body = client.get(f"/v3/deployments/{deployment_id}", headers=headers).json()
        assert body["status"] == "queued"


def test_a_deploy_can_be_narrowed_to_some_edges(settings):
    with TestClient(control_plane_app(settings)) as client:
        accepted = client.post(
            "/v3/deployments",
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
                "/v3/deployments",
                json={"host_limit": pattern},
                headers=API_HEADERS,
            )
            assert response.status_code == 422, pattern
        assert client.get("/v3/deployments", headers=API_HEADERS).json()["items"] == []


def test_drift_queues_a_check_run_and_reads_back_as_a_report(settings):
    with TestClient(control_plane_app(settings)) as client:
        queued = client.post("/v3/drift", json={}, headers=API_HEADERS)
        assert queued.status_code == 202
        assert queued.json()["check_mode"] is True

        deployment_id = queued.json()["id"]
        for _ in range(50):
            report = client.get(
                f"/v3/deployments/{deployment_id}/drift", headers=API_HEADERS
            )
            if report.status_code == 200:
                break
            time.sleep(0.05)
        assert report.status_code == 200
        assert report.json()["deployment_id"] == deployment_id


def test_an_applied_deployment_is_not_readable_as_drift(settings):
    with TestClient(control_plane_app(settings)) as client:
        queued = client.post(
            "/v3/deployments", json={"check": False}, headers=API_HEADERS
        )
        deployment_id = queued.json()["id"]
        response = client.get(
            f"/v3/deployments/{deployment_id}/drift", headers=API_HEADERS
        )
        assert response.status_code == 409


def test_a_single_site_is_readable_by_name(settings):
    with TestClient(control_plane_app(settings)) as client:
        seed_site_over_http(client, API_HEADERS)
        name = client.get("/v3/hosts", headers=API_HEADERS).json()["items"][0]["name"]

        response = client.get(f"/v3/hosts/{name}", headers=API_HEADERS)

        assert response.status_code == 200
        assert response.json()["name"] == name
        # The defaults the create body never mentioned.
        assert response.json()["ssl_mode"] == "off"
        assert response.json()["ssl_automatic_mode"] == "auto"
        assert "origin_scheme" not in response.json()


def test_removed_origin_scheme_is_rejected_on_create(
    settings, domain_payload, record_create_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/domains", json=domain_payload, headers=API_HEADERS)
        response = client.post(
            "/v3/domains/example.com/records",
            json={**record_create_payload, "origin_scheme": "https"},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_scheme_is_rejected_on_patch(
    settings, domain_payload, record_create_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/domains", json=domain_payload, headers=API_HEADERS)
        client.post(
            "/v3/domains/example.com/records",
            json=record_create_payload,
            headers=API_HEADERS,
        )
        response = client.patch(
            "/v3/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_scheme": "http"},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_create(
    settings, domain_payload, record_create_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/domains", json=domain_payload, headers=API_HEADERS)
        response = client.post(
            "/v3/domains/example.com/records",
            json={**record_create_payload, "origin_port": 8080},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_removed_origin_port_is_rejected_on_patch(
    settings, domain_payload, record_create_payload
):
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/domains", json=domain_payload, headers=API_HEADERS)
        client.post(
            "/v3/domains/example.com/records",
            json=record_create_payload,
            headers=API_HEADERS,
        )
        response = client.patch(
            "/v3/domains/example.com/records/cdn",
            params={"type": "A"},
            json={"origin_port": 8080},
            headers=API_HEADERS,
        )
        assert response.status_code == 422


def test_an_unknown_site_is_a_404(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/hosts/absent", headers=API_HEADERS).status_code == 404


# ----------------------------------------------------------------------
# Error mapping. A permanent condition must not be reported as transient: a
# client retrying a ConfigurationError retries forever, and a 503 alert pages
# someone about a controller correctly declining bad input.
# ----------------------------------------------------------------------


# Raised from a core operation, since these assert core's exception handlers.


def test_a_configuration_error_is_a_400_rather_than_a_503(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "update_edge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConfigurationError("edge does not exist: edge-01")
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        response = client.patch(
            "/v3/edges/edge-01", json={"port": 22}, headers=API_HEADERS
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "edge does not exist: edge-01"


def test_an_execution_error_is_a_502(settings, monkeypatch):
    monkeypatch.setattr(
        EdgeOperationsService,
        "update_edge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ExecutionError("ansible would not start")
        ),
    )
    with TestClient(control_plane_app(settings)) as client:
        response = client.patch(
            "/v3/edges/edge-01", json={"port": 22}, headers=API_HEADERS
        )
    assert response.status_code == 502


def test_a_busy_deployment_is_a_409_that_says_when_to_come_back(settings, monkeypatch):
    """The one conflict that clears on its own, so the one that gets Retry-After."""

    def busy(_self, _operator, **_kwargs):
        raise DeploymentBusyError("another deployment is already running")

    monkeypatch.setattr(DeploymentService, "submit_deployment", busy)
    with TestClient(control_plane_app(settings)) as client:
        response = client.post("/v3/deployments", json={}, headers=API_HEADERS)
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "30"


# ----------------------------------------------------------------------
# Edges. Writing one here puts a host into the Ansible inventory.
# ----------------------------------------------------------------------


_EDGE = {
    "name": "edge-01",
    "host": "192.0.2.10",
    "ssh_sources": ["198.51.100.0/24"],
}


def test_edge_crud_over_http(settings):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/edges", headers=API_HEADERS).json()["items"] == []

        created = client.post("/v3/edges", json=_EDGE, headers=API_HEADERS)
        assert created.status_code == 201
        assert created.json()["name"] == "edge-01"
        # Defaults are resolved by the model, not left for the feature to guess.
        assert created.json()["user"] == "deploy"
        assert created.json()["port"] == 22

        assert (
            client.post("/v3/edges", json=_EDGE, headers=API_HEADERS).status_code == 409
        )
        assert len(client.get("/v3/edges", headers=API_HEADERS).json()["items"]) == 1
        assert (
            client.get("/v3/edges/edge-01", headers=API_HEADERS).json()["host"]
            == "192.0.2.10"
        )
        assert client.get("/v3/edges/absent", headers=API_HEADERS).status_code == 404

        patched = client.patch(
            "/v3/edges/edge-01",
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
                "/v3/edges/absent", json={"port": 22}, headers=API_HEADERS
            ).status_code
            == 404
        )


def test_an_edge_patch_cannot_rename_the_host(settings):
    """The name is how certificates, audit history and `--limit` refer to it.

    `EdgePatch` has no name field and forbids extras, so a rename is a 422.
    """
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/edges", json=_EDGE, headers=API_HEADERS)

        response = client.patch(
            "/v3/edges/edge-01", json={"name": "edge-99"}, headers=API_HEADERS
        )

        assert response.status_code == 422


def test_an_invalid_management_cidr_is_a_422_not_a_stored_edge(settings):
    """Validation is the model's, so the API rejects exactly what the CLI does."""
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/edges",
            json={**_EDGE, "ssh_sources": ["anywhere"]},
            headers=API_HEADERS,
        )

        assert response.status_code == 422
        assert client.get("/v3/edges", headers=API_HEADERS).json()["items"] == []


def _queued_removal(settings, monkeypatch, path):
    """DELETE an edge, run the queued removal as the worker would, and read it."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/edges", json=_EDGE, headers=API_HEADERS)
        queued = client.delete(path, headers=API_HEADERS)
        assert queued.status_code == 202, queued.text
        run_queued_operations(control)
        operation = client.get(
            f"/v3/operations/{queued.json()['id']}", headers=API_HEADERS
        ).json()
        edges = [
            edge["name"]
            for edge in client.get("/v3/edges", headers=API_HEADERS).json()["items"]
        ]
    return operation, edges


def test_removing_an_edge_without_decommissioning_says_so(settings, monkeypatch):
    """The narrow case, and the result has to be honest about it.

    `decommission=false` leaves configuration and TLS private keys on the host,
    so the result reports which kind of removal happened.
    """
    operation, edges = _queued_removal(
        settings, monkeypatch, "/v3/edges/edge-01?decommission=false"
    )

    assert operation["status"] == "succeeded"
    assert operation["result"] == {
        "name": "edge-01",
        "decommissioned": False,
        "hosts": [],
    }
    assert edges == []


def test_a_failed_teardown_keeps_the_edge_registered(settings, monkeypatch):
    """Deregistering first would strand the host.

    The inventory is derived from these rows, so a host removed before teardown
    succeeded could never be addressed again.
    """
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (_ for _ in ()).throw(
            ExecutionError("teardown of edge-01 failed")
        ),
    )

    operation, edges = _queued_removal(settings, monkeypatch, "/v3/edges/edge-01")

    assert operation["status"] == "failed"
    assert operation["error"] == "teardown of edge-01 failed"
    assert edges == ["edge-01"]


def test_removing_an_unknown_edge_is_refused_before_queueing(settings):
    with TestClient(control_plane_app(settings)) as client:
        response = client.delete("/v3/edges/nowhere", headers=API_HEADERS)
    assert response.status_code == 404


def test_registering_an_edge_is_audited(settings):
    """Unanswerable before: edges never went through a service, so never a bus."""
    with TestClient(control_plane_app(settings)) as client:
        client.post("/v3/edges", json=_EDGE, headers=API_HEADERS)
        client.patch("/v3/edges/edge-01", json={"port": 7845}, headers=API_HEADERS)

        events = client.get("/v3/audit-events", headers=API_HEADERS).json()["items"]

        actions = [event["action"] for event in events]
        assert "edge.added" in actions
        assert "edge.updated" in actions
        update = next(event for event in events if event["action"] == "edge.updated")
        assert update["details"]["fields"] == ["port"]


def test_edge_routes_require_authentication(settings):
    """Every edge route is a control route; none may be read unauthenticated."""
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/edges").status_code == 401
        assert client.post("/v3/edges", json=_EDGE).status_code == 401
        assert client.patch("/v3/edges/edge-01", json={"port": 22}).status_code == 401
        assert client.delete("/v3/edges/edge-01").status_code == 401


def test_a_successful_teardown_reports_what_it_did(settings, monkeypatch):
    """The operation's result carries what each host's teardown did."""
    monkeypatch.setattr(
        EdgeOperationsService,
        "decommission_edge",
        lambda self, name, operator, force=False: (
            host_run("edge-01", changes=("remove /etc/blitzecdn",)),
        ),
    )

    operation, _ = _queued_removal(settings, monkeypatch, "/v3/edges/edge-01")

    result = operation["result"]
    assert result["decommissioned"] is True
    assert [host["host"] for host in result["hosts"]] == ["edge-01"]
    assert result["hosts"][0]["changes"][0]["task"] == "remove /etc/blitzecdn"


def test_no_cloudflare_header_name_is_published_by_the_api(settings):
    """The BZ- namespace is the whole surface; CF- and True-Client-IP are not
    ours to define and must not appear as fields, defaults, or descriptions."""
    with TestClient(control_plane_app(settings)) as client:
        document = client.get("/openapi.json").text

    for foreign in ("CF-Connecting-IP", "cf_connecting_ip", "True-Client-IP"):
        assert foreign not in document


def test_http3_create_read_patch_and_validation(settings):
    site = {
        "ssl_mode": "flexible",
        "http3_enabled": True,
        "certificate_mode": "existing",
        "certificate_path": "/etc/ssl/certs/edge.pem",
        "certificate_key_path": "/etc/ssl/private/edge.key",
    }
    with TestClient(control_plane_app(settings)) as client:
        created = _zone(client, **site)
        assert created.status_code == 201
        assert created.json()["http3_enabled"] is True
        assert (
            client.get("/v3/hosts", headers=API_HEADERS).json()["items"][0][
                "http3_enabled"
            ]
            is True
        )

        unchanged = client.patch(
            "/v3/domains/example.com",
            json={"http3_enabled": True},
            headers=API_HEADERS,
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["http3_enabled"] is True

        disabled = client.patch(
            "/v3/domains/example.com",
            json={"http3_enabled": False},
            headers=API_HEADERS,
        )
        assert disabled.status_code == 200
        assert disabled.json()["http3_enabled"] is False

        rejected = client.patch(
            "/v3/domains/example.com",
            json={"ssl_mode": "off", "http3_enabled": True},
            headers=API_HEADERS,
        )
        assert rejected.status_code == 422
        assert "requires ssl_mode" in rejected.text

        events = client.get("/v3/audit-events", headers=API_HEADERS).json()["items"]
        updates = [event for event in events if event["action"] == "domain.updated"]
        assert any("http3_enabled" in event["details"]["fields"] for event in updates)


def test_under_attack_mode_is_visible_patchable_and_in_openapi(settings):
    with TestClient(control_plane_app(settings)) as client:
        schema = client.get("/openapi.json").json()
        property_schema = schema["components"]["schemas"]["DomainPatch"]["properties"][
            "under_attack_mode"
        ]
        assert property_schema["type"] == "boolean"

        created = _zone(client)
        assert created.status_code == 201
        assert created.json()["under_attack_mode"] is False

        patched = client.patch(
            "/v3/domains/example.com",
            json={"under_attack_mode": True},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["under_attack_mode"] is True
        assert (
            client.get("/v3/hosts", headers=API_HEADERS).json()["items"][0][
                "under_attack_mode"
            ]
            is True
        )

        invalid = client.patch(
            "/v3/domains/example.com",
            json={"under_attack_mode": "sometimes"},
            headers=API_HEADERS,
        )
        assert invalid.status_code == 422


def test_max_upload_size_is_reported_patchable_and_validated(settings):
    with TestClient(control_plane_app(settings)) as client:
        created = _zone(client)
        assert created.status_code == 201
        assert created.json()["max_upload_size"] == "100m"

        patched = client.patch(
            "/v3/domains/example.com",
            json={"max_upload_size": "200m"},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["max_upload_size"] == "200m"

        # A size nginx would accept but the tier list does not name.
        rejected = client.patch(
            "/v3/domains/example.com",
            json={"max_upload_size": "500m"},
            headers=API_HEADERS,
        )
        assert rejected.status_code == 422

        # A patch that does not name the field leaves it where it was.
        client.patch(
            "/v3/domains/example.com",
            json={"cache_enabled": False},
            headers=API_HEADERS,
        )
        assert (
            client.get("/v3/hosts", headers=API_HEADERS).json()["items"][0][
                "max_upload_size"
            ]
            == "200m"
        )


def test_compression_is_reported_patchable_and_validated(settings):
    with TestClient(control_plane_app(settings)) as client:
        created = _zone(client)
        assert created.status_code == 201
        assert created.json()["compression"] == "brotli"

        patched = client.patch(
            "/v3/domains/example.com",
            json={"compression": "off"},
            headers=API_HEADERS,
        )
        assert patched.status_code == 200
        assert patched.json()["compression"] == "off"

        rejected = client.patch(
            "/v3/domains/example.com",
            json={"compression": "deflate"},
            headers=API_HEADERS,
        )
        assert rejected.status_code == 422

        # A patch that does not name the field leaves it where it was.
        client.patch(
            "/v3/domains/example.com",
            json={"cache_enabled": False},
            headers=API_HEADERS,
        )
        assert (
            client.get("/v3/hosts", headers=API_HEADERS).json()["items"][0][
                "compression"
            ]
            == "off"
        )


def test_visitor_headers_are_reported_replaced_wholesale_and_validated(settings):
    with TestClient(control_plane_app(settings)) as client:
        created = _zone(client)
        assert created.status_code == 201
        assert created.json()["visitor_headers"] == {
            "connecting_ip": True,
            "ip_country": False,
        }

        patched = client.patch(
            "/v3/domains/example.com",
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
            "/v3/domains/example.com",
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
                "/v3/domains/example.com",
                json={"visitor_headers": unknown},
                headers=API_HEADERS,
            )
            assert rejected.status_code == 422
