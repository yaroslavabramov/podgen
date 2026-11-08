"""Checklist-free current wires; historical data and pinned requests remain lossless."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.writing import ActPlan, Ending
from podgen.persistence.editorial_inputs import EditorialMaterials, generation_repo
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.models.contracts import WireReply
from podgen.writing.editorial_contract import EditorialInput, FixedEditorialInput
from podgen.writing.plan_views import candidate_view
from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput
from podgen.writing.planning_v9 import PlanningOutputV9
from podgen.writing.projection_versions import V3, V8
from podgen.writing.projections import creative_knowledge_view
from tests.historical_revisions import historical_revision_manifest
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider, setup_review
from tests.test_listener_question_flow import QuestionFlowProvider, current_planner_reply
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_planning_roles import review_payload as review_payload

ROOT = Path(__file__).resolve().parents[2]


def snow_plan():
    return json.loads((ROOT / "evals/reports/2026-11-06-snow-investigation-opening/plan.json").read_text())


def test_new_and_legacy_plan_shapes_roundtrip_without_manufacturing_or_losing_fields(review_payload):
    old = snow_plan()
    assert len(old["plan"]["acts"]) == 6
    assert all(act["limitations_to_voice"] for act in old["plan"]["acts"])
    assert old["plan"]["ending"]["qualifications"]
    assert PlanningOutput.model_validate_json(json.dumps(old)).model_dump(mode="json") == old
    historical = candidate_view(old, projection_version=V8)
    assert PlanningOutput.model_validate(historical).model_dump(mode="json") == historical
    new = current_planner_reply(old)
    assert PlanningOutputV9.model_validate(new).model_dump(mode="json") == new
    assert new["plan"]["working_answer"] == old["plan"]["working_answer"]
    assert "last_moment" in new["plan"]["ending"]
    assert "limitations" not in new
    assert all("limitations_to_voice" not in act for act in new["plan"]["acts"])
    assert "qualifications" not in new["plan"]["ending"]
    assert PlanReviewOutput.model_validate(review_payload).model_dump(mode="json") == review_payload
    review_payload.pop("important_limits")
    assert PlanReviewOutput.model_validate(review_payload).model_dump(mode="json") == review_payload
    # The other backend's canonical plan accepts the same new absence, preserving historical lists.
    act = deepcopy(new["plan"]["acts"][0])
    act["plan_act_id"] = str(uuid4())
    act.pop("act_id")
    act["material_refs"] = []
    assert ActPlan.model_validate(act).model_dump(mode="json") == act
    legacy = dict(act, limitations_to_voice=["  Полный исторический текст.\n" * 1000])
    assert ActPlan.model_validate(legacy).model_dump(mode="json") == legacy
    assert (
        Ending.model_validate(historical["plan"]["ending"]).model_dump(mode="json")
        == historical["plan"]["ending"]
    )
    assert Ending.model_validate(old["plan"]["ending"]).model_dump(mode="json") == old["plan"]["ending"]


def test_published_current_schemas_and_real_model_contract_have_no_obligation_lists():
    for stage, name in (("plan", "planning-output"), ("plan-review", "plan-review-output")):
        schema = response_schema(stage, "editorial-role-packets/1")
        assert schema == json.loads((ROOT / f"backend/schemas/{name}.current.schema.json").read_text())
        for definition in [schema, *schema.get("$defs", {}).values()]:
            if definition.get("type") == "object":
                assert set(definition["required"]) == set(definition["properties"])
                assert not {
                    "limitations_to_voice",
                    "qualifications",
                    "limitations",
                    "important_limits",
                } & set(definition["properties"])
    assert FixedEditorialInput.model_json_schema() == json.loads(
        (ROOT / "backend/schemas/fixed-editorial-input.current.schema.json").read_text()
    )
    # The published historical strict input schema remains in place.
    legacy = json.loads((ROOT / "backend/schemas/fixed-editorial-input.v1.schema.json").read_text())
    assert "limitations_to_voice" in legacy["$defs"]["PlanningAct"]["required"]


def test_actual_six_act_snow_author_and_dual_revision_wires_keep_support_not_recitation(own_dataset):
    repo, project, _, writer, _ = own_dataset
    packet = EditorialInput.model_validate_json(
        (ROOT / "evals/editorial-inputs/current/snow-pump/legacy-shared.json").read_bytes()
    )
    materials = EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())
    profile = load_profile(generation_repo(repo), project, writer, "draft")
    plan = snow_plan()
    original = deepcopy(plan)
    wire = preview_request(uuid4(), "draft", profile, materials, approved_plan=plan)
    author = payload(wire)
    assert "necessary_limits" not in author and "limitations" not in author["story"]
    assert author["story"]["episode_answer"] == plan["plan"]["working_answer"]
    assert author["story"]["ending"] is None
    assert author["story"]["duration_strategy"] == dict(
        estimated_seconds_range=plan["plan"]["duration_strategy"]["estimated_seconds_range"]
    )
    assert all("limitations_to_voice" not in act for act in author["story"]["acts"])
    assert author["assignment"]["required_topics"] == list(packet.assignment.required_topics)
    expected = creative_knowledge_view(packet, labels=False)
    assert all(card in expected for card in author["support"])
    script = dict(
        utterances=[dict(speaker=packet.assignment.speakers[0].label, text="Точный произносимый текст.")]
    )
    finding = dict(
        dimension="factuality",
        priority="important",
        anchors=[dict(utterance_index=0, quote=script["utterances"][0]["text"])],
        observation="Конкретное замечание.",
        requested_change="Уточнить условие вывода.",
        preserve=["Точное условие вместе с опорой."],
    )
    common = dict(
        listener_answer="Понимание текста.",
        important_limits=["Оценка только по тексту, не по записи."],
        strengths=[],
        findings=[finding],
        research_requests=[],
        duration_note="Длительность не измерена.",
        recommendation="revise",
    )
    reader = dict(common, reading_mode="script_only")
    fact = dict(
        common,
        reading_mode="informed",
        findings=[dict(finding, support_labels=[materials.material_catalog[0]])],
    )
    revised = payload(
        preview_request(
            uuid4(),
            "revise-1",
            profile,
            materials,
            approved_plan=plan,
            draft=script,
            reader_feedback=reader,
            fact_feedback=fact,
        )
    )
    assert set(revised["editorial_frame"]) == {
        "main_question",
        "listener_promise",
        "unresolved_questions",
        "episode_answer",
        "ending",
        "route_summary",
        "authority",
    }
    assert revised["editorial_frame"]["episode_answer"] == plan["plan"]["working_answer"]
    assert revised["editorial_frame"]["ending"] is None
    assert "necessary_limits" not in revised
    assert all("important_limits" not in row for row in revised["revision_guidance"])
    assert revised["revision_guidance"][1]["findings"][0] == fact["findings"][0]
    assert revised["repair_evidence"] and revised["support"]
    assert plan == original and reader["important_limits"] == common["important_limits"]


class CurrentPlanProvider(ReadingProvider):
    break_target = False

    def submit(self, body, *, client_request_id):
        if body["text"]["format"]["name"] == "plan_target_choices":
            self.requests.append(deepcopy(body))
            data = payload(body)
            assert not any("limitations_to_voice" in pointer for pointer in data["allowed_review_targets"])
            result = dict(
                choices=[
                    dict(target_id=row["target_id"], selected_path="/plan/main_question")
                    for row in data["invalid_targets"]
                ]
            )
            return WireReply(
                json.dumps(
                    dict(
                        id="resp_target_repair",
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=[
                            dict(
                                type="message",
                                role="assistant",
                                content=[dict(type="output_text", text=json.dumps(result))],
                            )
                        ],
                        usage=dict(input_tokens=100, output_tokens=100),
                    )
                ).encode()
            )
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"].startswith("plan-review"):
            response = json.loads(reply.body)
            content = response["output"][0]["content"][0]
            review = json.loads(content["text"])
            if self.break_target and body["metadata"]["podgen_stage"] == "plan-review":
                review["findings"][0]["targets"] = ["/missing/field"]
            content["text"] = json.dumps(review, ensure_ascii=False)
            return WireReply(json.dumps(response, ensure_ascii=False).encode())
        return reply


@pytest.mark.parametrize("repair_target", [False, True])
def test_ordinary_new_plan_review_revision_author_and_dual_revision_without_obligation_fields(
    own_dataset, tmp_path, monkeypatch, repair_target
):
    prefix, _, own, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    own["review_mode"] = "independent-readings/1"
    old = json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/fixed-plan.json").read_text())["plan"]
    initial = current_planner_reply(old)
    script = json.loads(
        (ROOT / "evals/reports/2026-11-05-planning/own-plan/snow-pump/draft.json").read_text()
    )["output"]
    provider = CurrentPlanProvider(("revise", "keep"))
    provider.break_target = repair_target
    provider.upstream = QuestionFlowProvider(initial, script, "Как проверить основания решения?")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **own) == (2 if repair_target else 0)
    if repair_target:
        journal = EvaluationJournal(own_dataset[0], own_dataset[1])
        failed = journal.invocation(own["run_id"], "plan-review")
        assert (
            invoke(
                prefix,
                "resume",
                run_id=own["run_id"],
                repair_plan_targets=failed["invocation_id"],
                export_dir=tmp_path / "export",
            )
            == 0
        )
        assert [attempt["attempt_kind"] for attempt in journal.attempts(own["run_id"], "plan-review")] == [
            "role",
            "plan_target_repair",
        ]
    views = {body["metadata"]["podgen_stage"]: payload(body) for body in provider.requests}
    assert {"plan-revise", "plan-review-revised", "revise-1", "fact-review-1"} <= set(views)
    assert "limitations_to_voice" not in views["plan"]["assignment"]
    for stage in ("plan-review", "plan-revise", "plan-review-revised"):
        assert "limitations" not in views[stage]["candidate_plan"]
    for stage in ("plan-revise", "revise-1"):
        guidance = views[stage]["revision_guidance"]
        assert all(
            "important_limits" not in row for row in (guidance if isinstance(guidance, list) else [guidance])
        )
    assert "necessary_limits" not in views["draft"] and "necessary_limits" not in views["revise-1"]
    persisted = json.loads((tmp_path / "export/plan.json").read_text())["output"]
    assert persisted == initial
    assert persisted["plan"]["duration_strategy"] == old["plan"]["duration_strategy"]
    assert "important_limits" not in json.loads((tmp_path / "export/plan-review.json").read_text())["output"]
    assert "limitations_to_voice" not in (tmp_path / "export/generated-plan.md").read_text()


def test_v3_pending_dual_rewrite_resumes_exact_saved_request(own_dataset, tmp_path, monkeypatch):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    original = EvaluationJournal.create_run

    def pinned(journal, command, manifest):
        return original(journal, command, historical_revision_manifest(manifest, projection_version=V3))

    monkeypatch.setattr(EvaluationJournal, "create_run", pinned)
    provider = ReadingProvider(("revise", "keep"), pause="revise-1")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    request = journal.request(journal.invocation(options["run_id"], "revise-1"))
    assert "necessary_limits" in payload(request)
    assert all("important_limits" in row for row in payload(request)["revision_guidance"])
    provider.pause = None
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert provider.requests[2] == request
    assert len(provider.requests) == 5
