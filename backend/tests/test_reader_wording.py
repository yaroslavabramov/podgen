"""Wording-first changes wire order/descriptions, never the canonical review or historical requests."""

import json
from pathlib import Path

import pytest

from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.projection_versions import V1, V2, V3, V4, V5, V6, V7, V8
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import V1 as READER_V1
from podgen.writing.reader_schema import V2 as READER_V2
from podgen.writing.reader_schema import V3 as READER_V3
from podgen.writing.reader_schema import V4 as READER_V4
from podgen.writing.reader_schema import V5 as READER_V5
from podgen.writing.reader_schema import VERSION, feedback_model
from podgen.writing.reader_story import ORDER as STORY_ORDER
from podgen.writing.reader_wording import ORDER
from tests.test_model_wire_schema import assert_reference_nodes
from tests.test_script_handoff import wire_hash

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("version", [None, READER_V1, READER_V2, READER_V3, READER_V4])
def test_every_historical_reader_wire_stays_byte_identical_in_all_stages_and_projections(version):
    hashes = json.loads((ROOT / "backend/tests/fixtures/reader-schema-through-v4.hashes.json").read_text())
    for contract in ("editorial-role-packets/1", "legacy-author-materials/1"):
        for projection in (V1, V2, V3, V4, V5, V6, V7, V8):
            for stage in ("reader-review", "reader-review-1", "reader-review-2"):
                schema = response_schema(
                    stage, contract, projection_version=projection, reader_schema_version=version
                )
                assert wire_hash(schema) == hashes[str(version)]


def test_v5_schema_still_matches_the_tested_wording_first_wire():
    expected = json.loads(
        (ROOT / "evals/reports/2026-11-08-reader-voice/runs/wording-first/01/request.json").read_text()
    )["text"]["format"]["schema"]
    schema = response_schema("reader-review", "editorial-role-packets/1", reader_schema_version=READER_V5)
    assert wire_hash(schema) == wire_hash(expected)
    assert list(schema["properties"]) == schema["required"] == ORDER
    assert_reference_nodes(schema)
    assert feedback_model(READER_V5) is feedback_model(READER_V4)
    for stage in ("reader-review-1", "reader-review-2"):
        assert wire_hash(
            response_schema(stage, "editorial-role-packets/1", reader_schema_version=READER_V5)
        ) == wire_hash(schema)


def test_current_schema_reconstructs_the_story_before_assigning_structural_and_line_edits():
    assert VERSION == "listener-experience/6"
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        schema = response_schema(stage, "editorial-role-packets/1", reader_schema_version=VERSION)
        assert list(schema["properties"]) == schema["required"] == list(STORY_ORDER)
        assert "First inspect the actual spoken wording" not in str(schema)
        assert_reference_nodes(schema)
    assert feedback_model(VERSION) is feedback_model(READER_V4)


def test_worked_edits_are_bound_to_the_full_reader_prompt_only():
    from hashlib import sha256

    examples = ROOT / "backend/src/podgen/writing/prompts/examples/reader-edits.md"
    samples = examples.read_text()
    assert samples.count("- Draft:") == samples.count("- Assignment:") == samples.count("- Edited:") == 3
    prompt = load_reading_instructions("reader-review")
    assert prompt.body.endswith(samples)
    assert prompt.version == "reader-review/sha256:" + sha256(prompt.body.encode()).hexdigest()
    assert all(samples not in load_reading_instructions(role).body for role in ("fact-review", "dual-revise"))
    assert not any("\u0400" <= c <= "\u04ff" for c in prompt.body)
    assert all(word not in prompt.body.lower() for word in ("snow", "cholera", "wright"))
