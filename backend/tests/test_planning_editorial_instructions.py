"""Planning instruction freeze reaches fresh requests without rewriting saved pins."""

from uuid import uuid4

import pytest

from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.writing.author_reply import contract_pin
from podgen.writing.model_references import CATALOG_KEY, preview_references
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import load_instruction_set
from tests.test_v5_role_wire import fixture


@pytest.mark.parametrize("stage", ["plan", "plan-revise", "plan-review", "plan-review-revised"])
def test_current_planning_and_revision_requests_use_new_instructions_but_saved_requests_keep_their_pin(stage):
    materials, _, candidate, profile, _, _, _, review = fixture()
    inputs = {} if stage == "plan" else dict(candidate=candidate)
    if stage == "plan-revise":
        inputs["plan_review"] = review
    run_id = uuid4()
    fresh = preview_request(run_id, stage, profile, materials, **inputs)
    prompt_stage = "plan-review" if stage == "plan-review-revised" else stage
    instructions = load_instruction_set(materials.format, include_planning=True)
    assert fresh["instructions"] == instructions[prompt_stage].body
    for task in ("ordinary inferences", "why_end_here", "moral or emotional effect"):
        assert task in fresh["instructions"]
    if stage in ("plan", "plan-revise"):
        assert "marking detail as optional does not select the version" in fresh["instructions"]
        assert "new subject knowledge listeners need" in fresh["instructions"]
    else:
        assert "An optional-material list does not make this choice" in fresh["instructions"]

    refs = preview_references(materials, V9)
    manifest = dict(
        role_projection_version=V9,
        response_schemas={stage: fresh["text"]["format"]["schema"]},
        **{CATALOG_KEY: refs.as_payload()},
    )
    manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    pinned_body = "Exact archived planning instructions.\r\nPreserve this old pin.\n"
    saved = PreviewSnapshot(manifest=manifest, instructions=pinned_body)
    replay = preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs)
    assert replay == dict(fresh, instructions=pinned_body)
    assert preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs) == replay
