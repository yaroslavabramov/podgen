"""Published reader contracts must continue to accept the original addressed decisions."""

import json
from pathlib import Path

import pytest
from podgen.domain.canonical import digest
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.projection_versions import V8
from podgen.writing.reader_schema import feedback_model
from podgen.writing.roles import WriteOutput

FIXTURES = Path(__file__).with_name("fixtures") / "reader-voice"


def test_historical_reader_forms_remain_exactly_frozen():
    for version, expected in json.loads((FIXTURES / "schema-hashes.json").read_bytes()).items():
        schema = response_schema(
            "reader-review-2",
            "editorial-role-packets/1",
            projection_version=V8,
            reader_schema_version=None if version == "None" else version,
        )
        assert digest(schema) == expected


@pytest.mark.parametrize("decision", ["keep", "revise"])
def test_saved_reader_decision_validates_without_locator_repair(decision):
    case = json.loads((FIXTURES / f"{decision}.json").read_bytes())
    review = feedback_model("listener-experience/4").model_validate(case["review"])
    review.validate_against(WriteOutput.model_validate(case["script"]))
    assert review.model_dump(mode="json") == case["review"]
