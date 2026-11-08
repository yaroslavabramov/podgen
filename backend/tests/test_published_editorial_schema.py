"""Validate entire real packets against the published schema with an external validator."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.editorial_schema import packet_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "backend/schemas"
INPUTS = ROOT / "evals/editorial-inputs"


def read(path):
    return json.loads(path.read_text())


def validator(version):
    published = read(SCHEMAS / f"editorial-input.v{version}.schema.json")
    Draft202012Validator.check_schema(published)
    return Draft202012Validator(published)


@pytest.mark.parametrize("version", [1, 2])
def test_published_schema_matches_the_generator(version):
    assert validator(version).schema == packet_schema(version=version)


@pytest.mark.parametrize(
    ("version", "path"),
    [
        (1, SCHEMAS / "editorial-input.v1.example.json"),
        (1, INPUTS / "v1/snow-pump/shared.json"),
        (1, INPUTS / "v1/gps-clock/shared.json"),
        (1, INPUTS / "current/snow-pump/legacy-shared.json"),
        (2, INPUTS / "current/snow-pump/history/before-source-restoration-2026-11-08.json"),
        (2, INPUTS / "current/gps-clock/shared.json"),
        (2, INPUTS / "current/snow-pump/shared.json"),
    ],
)
def test_whole_real_current_and_historical_packets_validate_externally(version, path):
    packet = read(path)
    validator(version).validate(packet)
    assert EditorialInput.model_validate(packet).model_dump(mode="json") == packet


@pytest.mark.parametrize("selection", [[], [0]])
def test_optional_narrative_selection_allows_empty_or_explicit_choice(selection):
    packet = read(INPUTS / "current/snow-pump/shared.json")
    packet["dossier"]["knowledge_cards"][0]["narrative_excerpt_indices"] = selection
    validator(2).validate(packet)
    EditorialInput.model_validate(packet)


@pytest.mark.parametrize("selection", [None, [0, 0], [-1], [True], [1.5], ["0"], "0"])
def test_published_v2_rejects_invalid_narrative_selection_on_the_whole_case(selection):
    packet = read(INPUTS / "current/snow-pump/shared.json")
    packet["dossier"]["knowledge_cards"][0]["narrative_excerpt_indices"] = selection
    with pytest.raises(ValidationError):
        validator(2).validate(packet)


def test_v1_still_forbids_the_new_field_and_v2_keeps_it_optional_and_unbounded():
    legacy = read(INPUTS / "v1/snow-pump/shared.json")
    legacy["dossier"]["knowledge_cards"][0]["narrative_excerpt_indices"] = []
    with pytest.raises(ValidationError, match="Additional properties"):
        validator(1).validate(legacy)
    card = validator(2).schema["$defs"]["KnowledgeCard"]
    assert "narrative_excerpt_indices" not in card["required"]
    assert "maxItems" not in card["properties"]["narrative_excerpt_indices"]
    assert card["additionalProperties"] is False


def test_cross_field_excerpt_bounds_remain_an_import_validation():
    packet = deepcopy(read(INPUTS / "current/snow-pump/shared.json"))
    card = packet["dossier"]["knowledge_cards"][0]
    card["narrative_excerpt_indices"] = [len(card["excerpts"])]
    # Standard JSON Schema cannot compare an index against a sibling array's dynamic length.
    validator(2).validate(packet)
    with pytest.raises(ValueError, match="editorial_invalid_narrative_excerpt_selection"):
        EditorialInput.model_validate(packet)
