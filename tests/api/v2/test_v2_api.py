from fastapi.testclient import TestClient

from blitzecdn.api import create_app
from blitzecdn.api.v2_models import RecordPatchV2 as V2RecordPatch
from blitzecdn.api.v2_models import SitePolicyV2
from blitzecdn.domain.sites import SitePolicy


def test_v2_carries_every_policy_field_the_domain_has():
    """The live version is the one that must not silently omit a setting.

    v1 is projected on the way out, so a missing field there is deliberate. v2
    is not, and a knob an operator can set but never see would fail nowhere
    else.
    """
    missing = set(SitePolicy.model_fields) - set(SitePolicyV2.model_fields)
    assert not missing, f"v2 does not expose {sorted(missing)}"

    unpatchable = set(SitePolicy.model_fields) - set(V2RecordPatch.model_fields)
    assert not unpatchable, f"v2 cannot PATCH {sorted(unpatchable)}"


def test_no_cloudflare_header_name_is_published_by_the_api(settings):
    """The BZ- namespace is the whole surface; CF- and True-Client-IP are not
    ours to define and must not appear as fields, defaults, or descriptions."""
    with TestClient(create_app(settings)) as client:
        document = client.get("/openapi.json").text

    for foreign in ("CF-Connecting-IP", "cf_connecting_ip", "True-Client-IP"):
        assert foreign not in document
