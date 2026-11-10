"""The current editorial commission changes descriptions, preserving all published schemas and prompts."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.behavior import resolve_behavior
from podgen.writing.reader_schema import (
    COMMISSION_VERSION,
    LISTENING_VERSION,
    RECORDING_VERSION,
    feedback_model,
)
from tests.schema_cases import without_descriptions
from tests.source_excerpt_cases import data_and_profile, release

HISTORICAL = [
    "cd3cb34dff240906a91da99a41f9d0100e8153036c813f4a8bea29f89d725cf9",
    "5593e2a6fbcb13b1d17cafd3eb25741c56d43038b38970ca651f0bfbf1e5a7a1",
    "b20d2c37dde4ad2dbf9eb72cd4ad70888d38fd821d9cbcac36baa999be424934",
    "4777ccd9445f8d3010111b9fbfde87b44d573bcfc740bf6a38ba356883b7b3a7",
    "ec9b0f1f585d36b5f113089aedace09b85c8e07c4d265a8124b5f526c50efbfa",
    "09263bf96c8b06943b008c9c22ff8223f3a7f84238fba0d699215132350b99b8",
    "0fb5c7e70e98dfde2c5c8d8ffc32641c32da76666cb42d4f6aa8e860e2305da8",
    "84a780e5b31e37d11212227997319c8bcce231d849d68028743facc2b3e268a8",
    "0e2f3976fed226de2824ac8e9e271f442f8ff6beb6b2618bffd609504862e744",
]


@pytest.mark.parametrize("version,expected", enumerate(HISTORICAL, start=1))
def test_every_published_reader_schema_is_unchanged(version, expected):
    schema = response_schema(
        "reader-review", "editorial-role-packets/1", reader_schema_version=f"listener-experience/{version}"
    )
    assert (
        hashlib.sha256(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        == expected
    )


def test_current_reader_preserves_shape_order_and_other_roles():
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        old = response_schema(stage, "editorial-role-packets/1", reader_schema_version=COMMISSION_VERSION)
        current = response_schema(stage, "editorial-role-packets/1", reader_schema_version=RECORDING_VERSION)
        assert json.dumps(without_descriptions(current)) == json.dumps(without_descriptions(old))
        assert current != old
    assert feedback_model(RECORDING_VERSION) is feedback_model(COMMISSION_VERSION)
    for stage in ("draft", "revise", "plan", "plan-review", "plan-revise", "fact-review"):
        assert response_schema(
            stage, "editorial-role-packets/1", reader_schema_version=RECORDING_VERSION
        ) == response_schema(stage, "editorial-role-packets/1", reader_schema_version=COMMISSION_VERSION)


def test_listening_addenda_keep_the_previous_criteria_and_response_contract():
    from podgen.writing.reader_listening import FIELDS, FINDING_FIELDS

    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        old = response_schema(stage, "editorial-role-packets/1", reader_schema_version=RECORDING_VERSION)
        current = response_schema(stage, "editorial-role-packets/1", reader_schema_version=LISTENING_VERSION)
        assert json.dumps(without_descriptions(current)) == json.dumps(without_descriptions(old))
        restored = deepcopy(current)
        for field in FIELDS:
            assert current["properties"][field]["description"].startswith(
                old["properties"][field]["description"]
            )
            restored["properties"][field]["description"] = old["properties"][field]["description"]
        for field in FINDING_FIELDS:
            assert current["$defs"]["EditorialFinding"]["properties"][field]["description"].startswith(
                old["$defs"]["EditorialFinding"]["properties"][field]["description"]
            )
            restored["$defs"]["EditorialFinding"]["properties"][field]["description"] = old["$defs"][
                "EditorialFinding"
            ]["properties"][field]["description"]
        assert restored == old
    assert feedback_model(LISTENING_VERSION) is feedback_model(RECORDING_VERSION)
    for stage in ("draft", "revise", "plan", "plan-review", "plan-revise", "fact-review"):
        assert response_schema(
            stage, "editorial-role-packets/1", reader_schema_version=LISTENING_VERSION
        ) == response_schema(stage, "editorial-role-packets/1", reader_schema_version=RECORDING_VERSION)


@pytest.mark.parametrize(
    "previous_version,previous_hash",
    [
        (COMMISSION_VERSION, "ec93938e9c60cd16d8eb05004bb617176327fc9c13c99dfec284a113a0c06e21"),
        (RECORDING_VERSION, "a86d0aabb38af129e3d774a0f21a90ec357ab00777c240329c118b5043d0e8b0"),
    ],
)
def test_frozen_previous_reader_replays_full_prompt_and_schema(monkeypatch, previous_version, previous_hash):
    materials, _, _, _, script, _, _, _, profile = data_and_profile()
    _, _, command, manifest = release(monkeypatch, materials, profile)
    assert manifest["reader_review_schema_version"] == LISTENING_VERSION
    previous = (
        Path(__file__).parents[1] / f"src/podgen/writing/behavior/assets/{previous_hash}.md"
    ).read_text()
    assert hashlib.sha256(previous.encode()).hexdigest() == previous_hash
    current = resolve_behavior().instructions("reader-review", materials.format).body
    assert previous != current
    old = deepcopy(manifest)
    old["reader_review_schema_version"] = previous_version
    stage = "reader-review"
    old["response_schemas"][stage] = response_schema(
        stage, command.input_contract, reader_schema_version=previous_version
    )
    saved = preview_request(
        command.run_id,
        stage,
        profile(stage),
        materials,
        snapshot=PreviewSnapshot(old, previous),
        draft=script,
    )
    fresh = preview_request(
        command.run_id,
        stage,
        profile(stage),
        materials,
        snapshot=PreviewSnapshot(manifest, current),
        draft=script,
    )
    assert saved["instructions"] == previous and fresh["instructions"] == current
    assert saved["text"]["format"]["schema"] == old["response_schemas"][stage]
    fresh["instructions"] = previous
    fresh["text"]["format"]["schema"] = saved["text"]["format"]["schema"]
    assert fresh == saved
