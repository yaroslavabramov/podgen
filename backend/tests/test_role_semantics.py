"""Versioned semantics, actual legacy wire, and ordinary CLI keep/revise delivery; no provider network."""

import hashlib
import json
from uuid import uuid4

import pytest
from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projection_versions import CURRENT, V1, V2, V3, V8, V9
from podgen.writing.projections import role_payload

from tests import FIXTURES
from tests.historical_revisions import historical_revision_manifest
from tests.independent_planner_provider import IndependentPlannerProvider
from tests.test_editorial_independence import invoke, payload
from tests.test_editorial_inputs import editorial as editorial

GPS_CASE = FIXTURES / "role-projection/gps"


def archived_materials():
    packet = EditorialInput.model_validate_json((GPS_CASE / "research-input.json").read_bytes())
    return EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())


def test_v1_projection_and_schemas_reproduce_real_legacy_wire():
    materials = archived_materials()
    plan = json.loads((GPS_CASE / "accepted-plan.json").read_text())
    expected = json.loads((GPS_CASE / "v1-request-hashes.json").read_text())
    for stage in ("plan", "draft"):
        view = role_payload(
            stage, materials, approved_plan=plan if stage == "draft" else None, projection_version=V1
        )
        payload = json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode()
        schema = response_schema(stage, "editorial-role-packets/1", projection_version=V1)
        assert hashlib.sha256(payload).hexdigest() == expected[stage]["payload_sha256"]
        assert (
            hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
            == expected[stage]["schema_sha256"]
        )


@pytest.mark.parametrize("projection", [V8, V9])
def test_legacy_plan_meaning_is_preserved_without_a_second_reference_script(projection):
    materials = archived_materials()
    plan = json.loads((GPS_CASE / "accepted-plan.json").read_text())
    original = json.dumps(plan, ensure_ascii=False)
    draft = json.loads((GPS_CASE / "draft.json").read_text())
    feedback = json.loads((GPS_CASE / "script-review.json").read_text())
    author = role_payload("draft", materials, approved_plan=plan, projection_version=projection)
    if projection == V8:
        assert author["story"]["conclusion"]["meaning"] == plan["plan"]["ending"]["answer_to_deliver"]
        assert "qualifications" not in author["story"]["conclusion"]
    else:
        assert author["story"]["episode_answer"] == plan["plan"]["working_answer"]
        assert author["story"]["ending"] is None
        assert "conclusion" not in author["story"]
    assert "limitations" not in author["story"]
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
            projection_version=projection,
        )
        assert (
            list(view).index("draft") < list(view).index("assignment") < list(view).index("editorial_frame")
        )
        assert "story_intent" not in view and "story" not in view
        assert view["editorial_frame"]["listener_promise"] == plan["plan"]["listener_promise"]
        assert "qualifications" not in view["editorial_frame"]
        assert "limitations" not in view["editorial_frame"]
        if projection == V8:
            assert plan["plan"]["ending"]["answer_to_deliver"] not in json.dumps(
                view["editorial_frame"], ensure_ascii=False
            )
        else:
            assert view["editorial_frame"]["episode_answer"] == plan["plan"]["working_answer"]
            assert view["editorial_frame"]["ending"] is None
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


@pytest.mark.parametrize("version", [V1, V2, V3])
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
        behavior_profile="existing/1",
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
        value = historical_revision_manifest(value, projection_version=version)
        value.pop("craft_reference", None)
        if version == V1:
            value.pop("role_projection_version")
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
    assert ("main_question" in payload(saved).get("editorial_frame", {})) == (version == V3)
    monkeypatch.setattr(provider, "count", count)
    monkeypatch.setattr(
        manifest,
        "resolve_behavior",
        lambda *args, **kwargs: pytest.fail("must use archived instructions"),
    )
    assert service.run(command)["status"] == "completed_unaccepted"
    assert journal.request(journal.invocation(command.run_id, "review")) == saved
    restored = journal.manifest(command.run_id)
    assert restored.get("role_projection_version", V1) == version
    assert "plan_selection_contract" not in restored and "writer_response_contract" not in restored


def test_v2_payloads_and_schemas_keep_exact_pre_upgrade_serialization():
    expected = json.loads((FIXTURES / "role-projection-v2.hashes.json").read_text())

    def read(name):
        return json.loads((GPS_CASE / name).read_text())

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


def test_v3_payloads_and_schemas_keep_exact_pre_upgrade_serialization():
    from copy import deepcopy

    from podgen.writing.reading_views import reading_payload

    expected = json.loads((FIXTURES / "role-projection-v3.hashes.json").read_text())
    materials = archived_materials()

    def read(name):
        return json.loads((GPS_CASE / name).read_text())

    initial, accepted, draft, review = map(
        read, ("initial-plan.json", "accepted-plan.json", "draft.json", "script-review.json")
    )
    inputs = {
        "plan": {},
        "plan-review": dict(candidate=initial),
        "plan-revise": dict(candidate=initial, plan_review=read("initial-plan-review.json")),
        "draft": dict(approved_plan=accepted),
        "review": dict(approved_plan=accepted, draft=draft),
        "revise": dict(approved_plan=accepted, draft=draft, feedback=review),
    }
    fact = deepcopy(review)
    for finding in fact["findings"]:
        finding["support_labels"] = [materials.material_catalog[0]]
    readings = dict(reader=dict(review, reading_mode="script_only"), fact=fact)

    def sha(value):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    for stage, hashes in expected["stages"].items():
        if stage in inputs:
            view = role_payload(stage, materials, projection_version=V3, **inputs[stage])
        else:
            args = dict(script=draft)
            if stage == "revise-1":
                args.update(
                    readings=readings,
                    decision=dict(
                        recommendation="revise", reason="required_changes_from_independent_readings"
                    ),
                    approved_plan=accepted,
                )
            view = reading_payload(
                "revise" if stage == "revise-1" else stage, materials, projection_version=V3, **args
            )
        assert sha(view) == hashes["payload_sha256"], stage
        assert (
            sha(response_schema(stage, "editorial-role-packets/1", projection_version=V3))
            == hashes["schema_sha256"]
        ), stage
