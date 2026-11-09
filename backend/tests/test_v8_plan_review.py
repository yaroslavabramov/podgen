"""V8 plan review assesses the enabled telling; historical fields and bytes remain frozen."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import CATALOG_KEY, ModelReferences, bind_schema, preview_references
from podgen.writing.plan_review_descriptions import FIELDS
from podgen.writing.projection_versions import V7, V8
from podgen.writing.projections import role_payload
from podgen.writing.reading_views import reading_payload

from tests.historical_preview import replay_preview as preview_request
from tests.own_plan_provider import proposal
from tests.test_card_ids import legacy
from tests.test_planning_schema_versions import without_descriptions
from tests.test_v5_role_wire import fixture


@pytest.mark.parametrize("stage", ["plan-review", "plan-review-revised"])
def test_v8_changes_only_model_facing_meaning_and_places_the_decision_after_the_assessment(stage):
    old = response_schema(stage, "editorial-role-packets/1", projection_version=V7)
    new = response_schema(stage, "editorial-role-packets/1", projection_version=V8)
    assert without_descriptions(new) == without_descriptions(old)
    assert list(new["properties"])[-2:] == ["decision_reason", "decision"]
    for name, description in FIELDS.items():
        assert new["properties"][name]["description"] == description
    assert new["required"] == old["required"]
    materials, _, plan, profile, *_ = fixture()
    snapshot = PreviewSnapshot(
        manifest=dict(
            role_projection_version=V8,
            response_schemas={stage: new},
            **{CATALOG_KEY: preview_references(materials, V8).as_payload()},
        ),
        instructions="Pinned V8 review instructions.",
    )
    request = preview_request(uuid4(), stage, profile, materials, candidate=plan, snapshot=snapshot)
    assert request["text"]["format"]["schema"] == bind_schema(new, preview_references(materials, V8))


@pytest.mark.parametrize("stage", ["plan-review", "plan-review-revised"])
def test_v8_matches_the_experiment_exactly_and_keeps_all_evidence_and_aliases(stage):
    root = Path(__file__).resolve().parents[2]
    packet = EditorialInput.model_validate_json(
        (root / "evals/editorial-inputs/current/snow-pump/shared.json").read_bytes()
    )
    refs = ModelReferences.create(packet)
    archived = json.loads((root / "evals/reports/2026-11-07-snow-short-editorial/planning.json").read_text())[
        "plan-revise"
    ]
    plan = refs.translate(archived.get("output", archived), inbound=True)
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    old = role_payload(stage, materials, candidate=plan, projection_version=V7)
    creative = role_payload("plan", materials, projection_version=V8)["dossier"]
    new = role_payload(stage, materials, candidate=plan, projection_version=V8)
    assert new == dict(old, dossier=creative)
    assert set(creative) == {"knowledge_cards"}
    critical_excerpt = next(
        excerpt.text
        for card in packet.dossier.knowledge_cards
        for excerpt in card.excerpts
        if "cannot now be dis-\ncovered" in excerpt.text
    )
    assert "cannot now be dis-\ncovered" in critical_excerpt
    assert any(passage["text"] == critical_excerpt for passage in new["evidence"])
    pinned = PreviewSnapshot(
        manifest=dict(
            role_projection_version=V8,
            response_schemas={
                stage: response_schema(stage, "editorial-role-packets/1", projection_version=V8)
            },
            **{CATALOG_KEY: refs.as_payload()},
        ),
        instructions="Pinned V8 review instructions.",
    )
    request = preview_request(uuid4(), stage, fixture()[3], materials, candidate=plan, snapshot=pinned)
    from tests.test_editorial_independence import payload

    assert payload(request) == refs.translate(new)
    script = dict(utterances=[dict(speaker=packet.assignment.speakers[0].label, text="Проверяемый текст.")])
    fact = reading_payload("fact-review", materials, script=script, projection_version=V8)
    assert fact == reading_payload("fact-review", materials, script=script, projection_version=V7)
    assert fact["support"][-1]["limitations"] == list(packet.dossier.knowledge_cards[-1].limitations)


def test_legacy_cards_without_story_facts_keep_the_full_supported_claim_and_context():
    packet = EditorialInput.model_validate(legacy())
    frozen = deepcopy(packet.model_dump(mode="json"))
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    plan = proposal(
        dict(material_catalog=materials.material_catalog, speaker_labels=materials.speaker_labels), "legacy"
    )
    for stage in ("plan-review", "plan-review-revised"):
        view = role_payload(stage, materials, candidate=plan, projection_version=V8)
        for actual, card in zip(
            view["dossier"]["knowledge_cards"], packet.dossier.knowledge_cards, strict=True
        ):
            assert actual["claim"] == card.claim
            assert actual["context"] == card.context
        assert (
            view["evidence"]
            == role_payload("plan-review", materials, candidate=plan, projection_version=V7)["evidence"]
        )
    assert packet.model_dump(mode="json") == frozen
