"""Native recheck forms and lossless cross-version listening on archived full episodes."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from podgen.domain.canonical import digest
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.role_request import compile_reading_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.evaluation.wire_budget import check_visible, visible_sizes
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projections import CONTRACT
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import VERSION as READER_VERSION
from podgen.writing.reader_schema import snapshot_model
from podgen.writing.revision import verification as checks

from tests import FIXTURES
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]
REVISION_CASES = FIXTURES / "revision-verification"


def reconstruct(comparison, current):
    result = []
    for block in comparison["previous_script_in_order"]:
        if "copy_current" in block:
            span = block["copy_current"]
            result.extend(current["utterances"][span["first"] : span["last"] + 1])
        else:
            result.extend(block["previous_turns"])
    return dict(utterances=result)


@pytest.mark.parametrize(
    "case,number",
    [("control-c", 1), ("control-c", 2), ("fresh-snow", 1), ("fresh-snow", 2), ("release-candidate", 1)],
)
def test_native_snow_recheck_contains_a_reconstructable_script_and_actual_editorial_assignments(case, number):
    root = REVISION_CASES / case
    stage = f"reader-review-{number}"
    previous = json.loads((root / ("draft.json" if number == 1 else "revise-1.json")).read_text())
    current = json.loads((root / f"revise-{number}.json").read_text())
    edit = json.loads((root / f"edit-{number}.json").read_bytes())
    packet = EditorialInput.model_validate_json(
        (
            ROOT / "evals/editorial-inputs/current/snow-pump/history/before-percolation-2026-11-08.json"
        ).read_bytes()
    )
    materials, profile = EditorialMaterials(packet, None, ()), fixture()[3]
    comparison = checks.comparison(previous, current, edit["revision_guidance"])
    request = compile_reading_request(
        uuid4(),
        stage,
        profile,
        materials,
        load_reading_instructions("revision-verification").body,
        response_schema(
            stage,
            CONTRACT,
            reader_schema_version=READER_VERSION,
            revision_verification_contract=checks.VERSION,
        ),
        script=current,
        audience=edit["audience"],
        revision_comparison=comparison,
    )
    check_visible(request, CONTRACT)
    visible = payload(request)
    assert [r["text"] for r in visible["script"]["utterances"]] == [r["text"] for r in current["utterances"]]
    result = visible.pop("revision_check")
    assert digest(visible) == edit["cold_payload_sha256"]
    assert reconstruct(result, current) == previous
    orders = {row["id"]: row for row in result["assignments"]}
    for review, prefix in zip(edit["revision_guidance"], ("R", "F"), strict=True):
        for i, finding in enumerate(review["findings"], 1):
            order = orders[f"{prefix}{i}"]
            assert order["requested_change"] == finding["requested_change"]
            assert order["observation"] == finding["observation"] and order["preserve"] == finding["preserve"]
    assert not {"support", "dossier", "editorial_frame", "evidence"} & set(visible)
    assert request["text"]["format"]["schema"]["$defs"]["AssignmentResult"]["properties"]["assignment_id"][
        "enum"
    ] == list(orders)
    print(
        case,
        number,
        "previous/current turns",
        len(previous["utterances"]),
        len(current["utterances"]),
        "assignments",
        len(orders),
        "bytes",
        visible_sizes(request)["total"],
    )


def test_first_and_fact_reading_forms_are_unchanged_and_prior_assessment_can_be_passed_to_author():
    for stage in ("reader-review", "fact-review", "fact-review-1", "fact-review-2"):
        kwargs = dict(reader_schema_version=READER_VERSION)
        assert response_schema(stage, CONTRACT, **kwargs) == response_schema(
            stage, CONTRACT, revision_verification_contract=checks.VERSION, **kwargs
        )
    prior = json.loads((REVISION_CASES / "prior-review.json").read_text())
    model = snapshot_model(prior)
    result = dict(
        prior,
        reading_mode="informed",
        effect_on_episode="Different ending.",
        assignment_results=[
            dict(
                assignment_id="R1",
                outcome="still_open",
                judgment="The new ending repeats the action.",
                followup_finding=0,
            )
        ],
    )
    assert snapshot_model(result) is checks.RevisionReading and model is not checks.RevisionReading
    recheck = checks.RevisionReading.model_validate(result)
    recheck.verify_assignments(dict(assignments=[dict(id="R1")]))
    for expected in ([], [dict(id="F1")], [dict(id="R1"), dict(id="F1")]):
        with pytest.raises(ValueError, match="assignments_mismatch"):
            recheck.verify_assignments(dict(assignments=expected))
    duplicate = checks.RevisionReading.model_validate(
        dict(result, assignment_results=result["assignment_results"] * 2)
    )
    with pytest.raises(ValueError, match="assignments_mismatch"):
        duplicate.verify_assignments(dict(assignments=[dict(id="R1")]))
    wrong = deepcopy(result)
    wrong["assignment_results"][0]["followup_finding"] = len(prior["findings"])
    with pytest.raises(ValueError, match="followup_mismatch"):
        checks.RevisionReading.model_validate(wrong).verify_assignments(dict(assignments=[dict(id="R1")]))


def test_whole_rewrite_may_reorder_duplicate_turns_or_change_every_line():
    def rows(words):
        return dict(utterances=[dict(speaker=speaker, text=text) for speaker, text in words])

    original = rows([("A", "X"), ("B", "X"), ("A", "Y"), ("A", "X")])
    for current in (
        rows([("B", "X"), ("A", "X"), ("A", "Y")]),
        rows([("A", "entirely different")]),
        original,
    ):
        view = checks.comparison(original, current, [])
        assert reconstruct(view, current) == original
