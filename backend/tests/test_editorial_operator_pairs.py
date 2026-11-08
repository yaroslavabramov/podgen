"""Export real current previews for local human comparisons; never judge their semantics."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from podgen.writing.plan_views import candidate_view
from podgen.writing.planning_roles import PlanningOutput
from podgen.writing.projection_versions import CURRENT
from podgen.writing.projections import actionable_feedback
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]
SUITE = json.loads((ROOT / "evals/operator/editorial-judgment/pairs.json").read_text())


def inputs(case):
    for binding in (SUITE["input"], SUITE["base_plan"]):
        assert hashlib.sha256((ROOT / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
    packet = EditorialInput.model_validate_json((ROOT / SUITE["input"]["path"]).read_bytes())
    refs = ModelReferences.create(packet)
    candidate = json.loads((ROOT / SUITE["base_plan"]["path"]).read_text())
    if "replacement" in case:
        target = candidate
        tokens = case["target"].split("/")[1:]
        for token in tokens[:-1]:
            target = target[int(token)] if isinstance(target, list) else target[token]
        assert tokens[-1] in target
        target[tokens[-1]] = case["replacement"]
        if "material_refs" in case:
            target["material_refs"] = case["material_refs"]
    candidate = refs.translate(candidate, inbound=True)
    PlanningOutput.model_validate(candidate).validate_against(
        material_locators=refs.card_ids, speaker_ids=[s.label for s in packet.assignment.speakers]
    )
    run_id = uuid5(NAMESPACE_URL, "podgen/operator-editorial-pairs/1/" + case["id"])
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=run_id), ())
    return run_id, materials, candidate


@pytest.mark.parametrize("case", SUITE["cases"], ids=lambda case: case["id"])
def test_local_operator_case_exports_actual_preview_without_annotations(case, tmp_path):
    run_id, materials, candidate = inputs(case)
    # A NEW current preview of a frozen legacy plan, not replay of a saved V8 request.
    # Existing offline compiler profile; no provider adapter, token counting, or model call.
    request = preview_request(run_id, "plan-review", fixture()[3], materials, candidate=candidate)
    refs = ModelReferences.create(materials.packet)
    assert payload(request)["candidate_plan"] == refs.translate(
        candidate_view(candidate, projection_version=CURRENT)
    )
    assert set(payload(request)) == {
        "output_language",
        "assignment",
        "dossier",
        "material_catalog",
        "candidate_plan",
        "review_target_root",
        "allowed_review_targets",
    }
    assert case["target"] in payload(request)["allowed_review_targets"]
    for name, content in dict(
        request=request,
        candidate=candidate,
        operator=case,
        provenance=dict(
            input=SUITE["input"],
            base_plan=SUITE["base_plan"],
            projection=CURRENT,
            references=refs.as_payload(),
            model_called=False,
            semantic_result="not_measured",
        ),
    ).items():
        (tmp_path / (name + ".json")).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("selection", ["one", "all"])
def test_no_fact_coverage_quota_or_automatic_caveat_insertion(selection):
    run_id, materials, candidate = inputs(SUITE["cases"][0])
    chosen = list(materials.material_catalog) if selection == "all" else [materials.material_catalog[0]]
    candidate["plan"]["opening"]["material_refs"] = chosen
    for act in candidate["plan"]["acts"]:
        act["material_refs"] = chosen
        act["prerequisite_concepts"] = []
    candidate["research_requests"] = []
    author = payload(preview_request(run_id, "draft", fixture()[3], materials, approved_plan=candidate))
    assert len(author["support"]) == len(chosen)
    assert all(
        {"claim", "sources"} <= set(card) <= {"claim", "sources", "source_excerpts"}
        for card in author["support"]
    )
    assert "necessary_limits" not in author
    assert all("limitations_to_voice" not in act for act in author["story"]["acts"])


def test_requirements_reach_author_through_accepted_plan_without_semantic_filter():
    review = json.loads((ROOT / "evals/reports/2026-11-07-snow-plan/02-editor.json").read_text())
    guidance = actionable_feedback(review, planning=True)
    assert guidance["findings"] == review["findings"]
    assert guidance["research_requests"] == review["research_requests"]
    run_id, materials, candidate = inputs(SUITE["cases"][0])
    imposed = next(case for case in SUITE["cases"] if case["id"] == "repeated-justification")["alternatives"][
        0
    ]
    # Simulate an adopted editorial instruction, not a model result or a reference script.
    revised = deepcopy(candidate)
    revised["plan"]["acts"][0]["intended_change_in_understanding"] = imposed
    revised["plan"]["acts"][0]["prerequisite_concepts"] = [imposed]
    revised["plan"]["duration_strategy"]["assumptions"] = [imposed]
    revised["research_requests"] = [
        dict(
            question=imposed,
            why_needed="Неблокирующее пожелание.",
            affected_act_ids=[],
            material_refs=[],
            blocking=False,
        )
    ]
    author = payload(preview_request(run_id, "draft", fixture()[3], materials, approved_plan=revised))
    assert author["story"]["acts"][0]["intended_change_in_understanding"] == imposed
    assert author["story"]["acts"][0]["prerequisite_concepts"] == [imposed]
    assert author["story"]["unresolved_questions"][0]["question"] == imposed
    assert "assumptions" not in author["story"]["duration_strategy"]
    assert "plan_review" not in author and "revision_guidance" not in author
