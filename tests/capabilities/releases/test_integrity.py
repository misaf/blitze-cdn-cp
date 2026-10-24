"""Release identity survives consumer edits and detects damaged persistence."""

from pathlib import Path

import pytest
from sqlmodel import select

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.releases.adapters.tables import ReleaseInputsRow, ReleaseRow
from blitzecdn.capabilities.releases.domain import Artifact, Release, ReleaseInputs
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConfigurationError


def test_artifact_detaches_both_constructor_input_and_decoded_views():
    document = {"sites": [{"enabled": True}]}
    artifact = Artifact.from_document(name="desired-state", document=document)
    release = Release(inputs_digest="example", artifacts=(artifact,))
    identity = release.digest

    document["sites"][0]["enabled"] = False
    artifact.document["sites"][0]["enabled"] = False

    assert release.digest == identity
    assert artifact.document == {"sites": [{"enabled": True}]}
    assert Release.model_validate_json(release.model_dump_json()) == release


@pytest.mark.parametrize(
    "value", [Path("material/key.pem"), float("nan"), {1: "invalid"}]
)
def test_artifacts_refuse_values_without_a_canonical_json_representation(value):
    with pytest.raises(ValueError):
        Artifact.from_document(name="invalid", document={"value": value})


def test_artifact_identity_is_independent_of_dictionary_order():
    assert (
        Artifact.from_document(name="a", document={"a": 1, "b": 2}).digest
        == Artifact.from_document(name="a", document={"b": 2, "a": 1}).digest
    )


def test_release_store_rejects_mismatched_inputs(tmp_path):
    with pytest.raises(ConfigurationError, match="inputs do not match"):
        repository = Repository(tmp_path / "state.db")
        try:
            repository.releases.save(Release(inputs_digest="wrong"), ReleaseInputs())
        finally:
            repository.close()


@pytest.mark.parametrize("damage_inputs", [False, True])
def test_release_store_detects_modified_documents(tmp_path, damage_inputs):
    repository = Repository(tmp_path / "state.db")
    try:
        inputs = ReleaseInputs()
        release = Release(inputs_digest=inputs.digest)
        repository.releases.save(release, inputs)
        with repository.database.session() as session:
            if damage_inputs:
                row = session.exec(select(ReleaseInputsRow)).one()
                row.document = ReleaseInputs(
                    domains=(Domain(name="example.com"),)
                ).encode()
            else:
                row = session.exec(select(ReleaseRow)).one()
                row.document = {**row.document, "compiler_version": 999}
            session.add(row)
        with pytest.raises(ConfigurationError, match="integrity verification"):
            if damage_inputs:
                repository.releases.inputs_for(release.id)
            else:
                repository.releases.get(release.id)
    finally:
        repository.close()


@pytest.mark.parametrize("retired_version", [1, 2])
def test_release_inputs_store_composed_policy_and_refuse_retired_schema(
    retired_version,
):
    import json

    from blitzecdn.capabilities.dns.domain import Domain
    from blitzecdn.capabilities.releases.domain import ReleaseInputs
    from blitzecdn.capabilities.serving.policy import SitePolicy

    inputs = ReleaseInputs.of(
        [Domain(name="example.com", policy=SitePolicy(cache={"cache_enabled": False}))],
        [],
        [],
    )
    document = json.loads(inputs.encode())
    assert document["domains"][0]["policy"]["cache"]["cache_enabled"] is False
    assert "cache_enabled" not in document["domains"][0]
    assert ReleaseInputs.decode(inputs.encode()) == inputs
    document["schema_version"] = retired_version
    with pytest.raises(ValueError, match="unsupported release inputs schema version"):
        ReleaseInputs.decode(json.dumps(document))
