"""Planning instruction freeze reaches fresh requests without rewriting saved pins."""

from uuid import uuid4

import pytest
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.writing.author_reply import contract_pin
from podgen.writing.model_references import CATALOG_KEY, preview_references
from podgen.writing.planning_choice import KEY, VERSION
from podgen.writing.planning_duration import KEY as DURATION_KEY
from podgen.writing.planning_duration import VERSION as DURATION_VERSION
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import load_instruction_set

from tests import FIXTURES
from tests.historical_preview import replay_preview as preview_request
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
        assert "select what will actually be developed at that scale" in fresh["instructions"]
        assert "new subject knowledge listeners need" in fresh["instructions"]
        assert "what listeners have already heard" in fresh["instructions"]
        assert "When uncertainty only narrows a claim, narrow it there" in fresh["instructions"]
    else:
        assert "A list of optional cuts cannot fix an overfull act" in fresh["instructions"]
        assert "what the earlier scenes have established" in fresh["instructions"]
        assert "read the affected sequence with just the precise claim in place" in fresh["instructions"]

    refs = preview_references(materials, V9)
    manifest = dict(
        role_projection_version=V9,
        response_schemas={stage: fresh["text"]["format"]["schema"]},
        **{CATALOG_KEY: refs.as_payload(), KEY: VERSION, DURATION_KEY: DURATION_VERSION},
    )
    manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    pinned_body = "Exact archived planning instructions.\r\nPreserve this old pin.\n"
    saved = PreviewSnapshot(manifest=manifest, instructions=pinned_body)
    replay = preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs)
    assert replay == dict(fresh, instructions=pinned_body)
    assert preview_request(run_id, stage, profile, materials, snapshot=saved, **inputs) == replay


def test_archived_snow_plan_review_replays_including_its_former_solo_writer_instructions():
    import json
    from pathlib import Path
    from uuid import UUID

    from podgen.domain.contracts.common import RuntimeRef
    from podgen.persistence.editorial_inputs import EditorialMaterials
    from podgen.writing.editorial_contract import EditorialInput

    evals = Path(__file__).resolve().parents[2] / "evals"
    archive = FIXTURES / "planning/legacy-solo-review"
    old = json.loads((archive / "request.json").read_bytes())
    candidate = json.loads((archive / "candidate.json").read_bytes())
    packet = EditorialInput.model_validate_json(
        (evals / "editorial-inputs/current/snow-pump/history/before-percolation-2026-11-08.json").read_bytes()
    )
    run_id = UUID(old["metadata"]["podgen_run_id"])
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=run_id), ())
    _, _, _, profile, *_ = fixture()
    profile.model_revision_or_alias = old["model"]
    profile.output_limit = old["max_output_tokens"]
    profile.generation_parameters.update(
        reasoning=old["reasoning"], text={"verbosity": old["text"]["verbosity"]}
    )
    refs = preview_references(materials, V9)
    manifest = {
        "role_projection_version": V9,
        "response_schemas": {"plan-review": old["text"]["format"]["schema"]},
        CATALOG_KEY: refs.as_payload(),
        KEY: VERSION,
    }
    manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    pinned = PreviewSnapshot(manifest, old["instructions"])
    replay = preview_request(run_id, "plan-review", profile, materials, snapshot=pinned, candidate=candidate)
    assert replay == old

    fresh = preview_request(run_id, "plan-review", profile, materials, candidate=candidate)
    assert "# Solo" in old["instructions"] and "# Solo" not in fresh["instructions"]

    def user_input(request):
        return json.loads(request["input"][0]["content"][0]["text"])

    old_input = user_input(old)
    new_input = user_input(fresh)
    for field in ("candidate_plan", "dossier", "assignment", "material_catalog", "allowed_review_targets"):
        assert new_input[field] == old_input[field]
    assert new_input["assignment"]["format"] == "solo"
    assert fresh["text"]["format"] == old["text"]["format"]
