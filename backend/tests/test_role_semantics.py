"""Versioned semantics, actual legacy wire, and ordinary CLI keep/revise delivery; no provider network."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.evaluation.workflow import stages_for
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projection_versions import CURRENT, V1, V2
from podgen.writing.projections import role_payload
from tests.independent_planner_provider import IndependentPlannerProvider
from tests.test_editorial_independence import invoke, payload
from tests.test_editorial_inputs import editorial as editorial

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "evals/reports/2026-11-06-gps-clean-run"


def archived_materials():
    packet = EditorialInput.model_validate_json((ARCHIVE / "research-input.json").read_bytes())
    return EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())


def test_v1_projection_and_schemas_reproduce_real_legacy_wire():
    materials = archived_materials()
    plan = json.loads((ARCHIVE / "accepted-plan.json").read_text())["output"]
    for stage, file in (("plan", "planner-request.json"), ("draft", "writer-request.json")):
        request = json.loads((ARCHIVE / file).read_text())
        before = role_payload(
            stage, materials, approved_plan=plan if stage == "draft" else None, projection_version=V1
        )
        assert (
            json.dumps(before, ensure_ascii=False, separators=(",", ":"))
            == request["input"][0]["content"][0]["text"]
        )
        assert (
            response_schema(stage, "editorial-role-packets/1", projection_version=V1)
            == request["text"]["format"]["schema"]
        )


def test_legacy_plan_meaning_is_preserved_without_a_second_reference_script():
    materials = archived_materials()
    plan = json.loads((ARCHIVE / "accepted-plan.json").read_text())["output"]
    original = json.dumps(plan, ensure_ascii=False)
    draft = json.loads((ARCHIVE / "draft.json").read_text())["output"]
    feedback = json.loads((ARCHIVE / "script-review.json").read_text())["output"]
    author = role_payload("draft", materials, approved_plan=plan)
    assert author["story"]["conclusion"]["meaning"] == plan["plan"]["ending"]["answer_to_deliver"]
    assert author["story"]["conclusion"]["qualifications"] == plan["plan"]["ending"]["qualifications"]
    assert author["story"]["listener_promise"] == plan["plan"]["listener_promise"]
    assert [v["interaction_purpose"] for v in author["story"]["speaker_contributions"]] == [
        v["interaction_guidance"] for v in plan["plan"]["host_functions"]
    ]
    for stage in ("review", "revise"):
        view = role_payload(
            stage,
            materials,
            approved_plan=plan,
            draft=draft,
            feedback=feedback if stage == "revise" else None,
        )
        assert (
            list(view).index("draft") < list(view).index("assignment") < list(view).index("editorial_frame")
        )
        assert "story_intent" not in view and "story" not in view
        assert view["editorial_frame"]["listener_promise"] == plan["plan"]["listener_promise"]
        assert view["editorial_frame"]["qualifications"] == plan["plan"]["ending"]["qualifications"]
        assert plan["plan"]["ending"]["answer_to_deliver"] not in json.dumps(
            view["editorial_frame"], ensure_ascii=False
        )
        assert view["draft"]["utterances"][-1]["text"] == draft["utterances"][-1]["text"]
    assert json.dumps(plan, ensure_ascii=False) == original


@pytest.mark.parametrize("mode", ["fixed-plan", "own-plan"])
@pytest.mark.parametrize("decision", ["keep", "revise"])
def test_current_ordinary_cli_preserves_author_speech_and_delivers_required_change(
    repo, editorial, tmp_path, monkeypatch, mode, decision
):
    from podgen.runtime.evaluation.profiles import create_profile

    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(
        repo,
        project,
        model="local-double",
        context_limit=1050000,
        output_limit=32768,
        roles=["writer", "editorial_reviewer", "planner", "plan_reviewer"],
    )
    provider = IndependentPlannerProvider(review_decision=decision)
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    ref = json.dumps(profile.ref.model_dump(mode="json"))
    run_id = uuid4()
    out = tmp_path / "run"
    args = dict(
        review_mode="informed-v1",
        run_id=run_id,
        case=data["assignment"]["key"],
        author_ref=json.dumps(shared.model_dump(mode="json")),
        writer_profile=ref,
        reviewer_profile=ref,
        plan_mode=mode,
        export_dir=out,
    )
    args.update(
        dict(fixed_plan_input_ref=json.dumps(fixed.model_dump(mode="json")))
        if mode == "fixed-plan"
        else dict(planner_profile=ref, plan_reviewer_profile=ref)
    )
    assert invoke(prefix, "run", **args) == 0
    assert json.loads((out / "manifest.json").read_text())["role_projection_version"] == CURRENT
    review = next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "review")
    assert payload(review)["draft"]["utterances"][0]["text"] == provider.script["utterances"][0]["text"]
    assert "story_intent" not in payload(review)
    if decision == "revise":
        revision = payload(provider.requests[-1])
        assert revision["revision_guidance"]["findings"] == provider.feedback["findings"]
        assert (
            revision["editorial_frame"]["listener_promise"]
            == payload(review)["editorial_frame"]["listener_promise"]
        )
        assert json.loads((out / "revise.json").read_text())["output"] == provider.revised_script
        assert provider.revised_script != provider.script
    else:
        assert not (out / "archive/revise").exists()
        assert json.loads((out / "final.json").read_text())["output"] == provider.script
    count = len(provider.requests)
    assert invoke(prefix, "resume", run_id=run_id, export_dir=out) == 0
    assert len(provider.requests) == count


@pytest.mark.parametrize("version", [V1, V2])
def test_older_manifest_resume_keeps_pinned_schema_and_request(
    repo, editorial, tmp_path, monkeypatch, version
):
    from podgen.runtime.evaluation import manifest
    from podgen.runtime.evaluation.contracts import WritingRunCommand
    from podgen.runtime.evaluation.profiles import create_profile
    from podgen.runtime.evaluation.service import WritingEvaluation

    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        review_mode="informed-v1",
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=fixed,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
    )
    original = EvaluationJournal.create_run

    def historical(journal, command, value):
        value = dict(value)
        value.pop("role_projection_version")
        if version == V2:
            value["role_projection_version"] = V2
        value["response_schemas"] = {
            stage: response_schema(stage, command.input_contract, projection_version=version)
            for stage in stages_for(command)
        }
        return original(journal, command, value)

    monkeypatch.setattr(EvaluationJournal, "create_run", historical)
    provider = IndependentPlannerProvider()
    count = provider.count

    def interrupted(body):
        if body["text"]["format"]["name"] == "editorial_feedback":
            raise KeyboardInterrupt()
        return count(body)

    monkeypatch.setattr(provider, "count", interrupted)
    service = WritingEvaluation(repo, lambda _: provider)
    with pytest.raises(KeyboardInterrupt):
        service.run(command)
    journal = EvaluationJournal(repo, project)
    saved = journal.request(journal.invocation(command.run_id, "review"))
    assert ("story_intent" in payload(saved)) == (version == V1)
    assert "main_question" not in payload(saved).get("editorial_frame", {})
    monkeypatch.setattr(provider, "count", count)
    monkeypatch.setattr(
        manifest,
        "load_instruction_set",
        lambda *args, **kwargs: pytest.fail("must use archived instructions"),
    )
    assert service.run(command)["status"] == "completed_unaccepted"
    assert journal.request(journal.invocation(command.run_id, "review")) == saved
    assert journal.manifest(command.run_id).get("role_projection_version", V1) == version


def test_v2_payloads_and_schemas_keep_exact_pre_upgrade_serialization():
    expected = json.loads((ROOT / "backend/tests/fixtures/role-projection-v2.hashes.json").read_text())

    def read(name):
        return json.loads((ARCHIVE / name).read_text())["output"]

    initial = read("initial-plan.json")
    feedback = read("initial-plan-review.json")
    accepted = read("accepted-plan.json")
    draft = read("draft.json")
    review = read("script-review.json")
    inputs = {
        "plan": {},
        "plan-review": dict(candidate=initial),
        "plan-revise": dict(candidate=initial, plan_review=feedback),
        "draft": dict(approved_plan=accepted),
        "review": dict(approved_plan=accepted, draft=draft),
        "revise": dict(approved_plan=accepted, draft=draft, feedback=review),
    }

    def digest(value):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    for stage, values in inputs.items():
        assert (
            digest(role_payload(stage, archived_materials(), projection_version=V2, **values))
            == expected["stages"][stage]["payload_sha256"]
        )
        assert (
            digest(response_schema(stage, "editorial-role-packets/1", projection_version=V2))
            == expected["stages"][stage]["schema_sha256"]
        )
