"""Short model aliases are exact version-bound routing; native IDs and raw replies remain intact."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.editorial_schema import packet_schema
from podgen.writing.model_references import (
    CATALOG_KEY,
    ModelReferences,
    bind_schema,
    preview_references,
    run_references,
)
from podgen.writing.projection_versions import V4, V5
from podgen.writing.projections import validate_editorial_content

ROOT = Path(__file__).resolve().parents[2]


def current_packet():
    return EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current/snow-pump/shared.json").read_bytes()
    )


def test_translation_is_directional_and_never_edits_quotes_or_guesses_strings():
    refs = ModelReferences("a" * 64, ("Cstable", "M1"))
    native = dict(
        card_id="M1",
        material_refs=["M1", "Cstable"],
        nested=dict(support_labels=["Cstable"]),
        quote="M1, M01 and Cstable are literal source words.",
    )
    wire = refs.translate(native)
    assert wire["card_id"] == "M2" and wire["material_refs"] == ["M2", "M1"]
    assert wire["quote"] == native["quote"]
    assert refs.translate(wire, inbound=True) == native
    assert native["material_refs"] == ["M1", "Cstable"]
    for value in ("M01", "M0", "m1", "M999", "M1 ", "Cstable"):
        with pytest.raises(ContractError, match="unknown_wire_material_reference"):
            refs.translate(dict(material_refs=[value]), inbound=True)
    with pytest.raises(ContractError, match="unknown_persisted_material_reference"):
        refs.translate(dict(material_refs=["not-a-card"]))


def test_catalogue_binds_names_order_facts_and_ids_to_the_exact_frozen_packet():
    packet = current_packet()
    materials = SimpleNamespace(packet=packet)
    refs = preview_references(materials, V5)
    assert preview_references(materials, V4) is None
    manifest = dict(role_projection_version=V5, **{CATALOG_KEY: refs.as_payload()})
    assert run_references(materials, manifest) == refs
    assert ModelReferences.restore(refs.as_payload()) == refs
    for change in ("order", "name", "meaning"):
        raw = packet.model_dump(mode="json")
        if change == "order":
            raw["dossier"]["knowledge_cards"].reverse()
        elif change == "name":
            raw["dossier"]["knowledge_cards"][0]["label"] += " changed"
        else:
            raw["dossier"]["knowledge_cards"][0]["context"] += " Changed context."
        changed = SimpleNamespace(packet=EditorialInput.model_validate(raw))
        with pytest.raises(ContractError, match="catalog_binding_mismatch"):
            run_references(changed, manifest)
    malformed = refs.as_payload()
    malformed["entries"][0]["ref"] = "M01"
    with pytest.raises(ContractError, match="invalid_model_material_catalog"):
        ModelReferences.restore(malformed)


def test_current_wire_schema_enumerates_only_aliases_and_old_schema_is_unchanged():
    refs = ModelReferences("a" * 64, ("Cfirst", "Csecond"))
    for stage, definition, field in (
        ("plan", "PlanningAct", "material_refs"),
        ("plan-review", "PlanReviewFinding", "material_refs"),
        ("fact-review", "FactFinding", "support_labels"),
    ):
        original = response_schema(stage, "editorial-role-packets/1", projection_version=V5)
        wire = bind_schema(original, refs)
        assert wire["$defs"][definition]["properties"][field]["items"]["enum"] == ["M1", "M2"]
        assert "enum" not in original["$defs"][definition]["properties"][field]["items"]
        assert bind_schema(original, None) == original


def test_optional_story_facts_are_full_v2_only_and_do_not_change_older_dumps():
    raw = current_packet().model_dump(mode="json")
    for card in raw["dossier"]["knowledge_cards"]:
        card.pop("story_facts", None)
    before = deepcopy(raw)
    assert (
        "story_facts"
        not in EditorialInput.model_validate(raw).model_dump(mode="json")["dossier"]["knowledge_cards"][0]
    )
    full = "  Full authored factual account, with its scope.\n" * 10000
    raw["dossier"]["knowledge_cards"][0]["story_facts"] = full
    parsed = EditorialInput.model_validate(raw)
    assert parsed.dossier.knowledge_cards[0].story_facts == full
    assert parsed.model_dump(mode="json")["dossier"]["knowledge_cards"][0]["story_facts"] == full
    assert digest(parsed.model_dump(mode="json")) != digest(before)
    for value in (None, "", "   "):
        broken = deepcopy(raw)
        broken["dossier"]["knowledge_cards"][0]["story_facts"] = value
        with pytest.raises(ValidationError):
            EditorialInput.model_validate(broken)
    legacy = json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/shared.json").read_text())
    assert EditorialInput.model_validate(legacy).model_dump(mode="json") == legacy
    for value in (None, full):
        legacy["dossier"]["knowledge_cards"][0]["story_facts"] = value
        with pytest.raises(ValidationError):
            EditorialInput.model_validate(legacy)
    assert "story_facts" not in packet_schema(version=1)["$defs"]["KnowledgeCard"]["properties"]
    card_schema = packet_schema(version=2)["$defs"]["KnowledgeCard"]
    assert card_schema["properties"]["story_facts"]["type"] == "string"
    assert "story_facts" not in card_schema["required"]


@pytest.mark.parametrize("technical", ["a" * 64, "11111111-2222-3333-4444-555555555555"])
def test_story_facts_receive_the_existing_technical_identity_validation(technical):
    raw = current_packet().model_dump(mode="json")
    raw["dossier"]["knowledge_cards"][0]["story_facts"] = "Source text: " + technical
    with pytest.raises(ContractError, match="technical_identity_in_role_input"):
        validate_editorial_content(EditorialInput.model_validate(raw), ())
