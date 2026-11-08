"""Real inputs through ordinary CLI; reviewed semantic boundary, not a universal classifier."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing.plan_selection import KEY as PLAN_SELECTION_KEY
from podgen.writing.plan_selection import VERSION as PLAN_SELECTION_VERSION
from tests.editorial_boundary import editorial_leaks, inject_reviewed_leaks
from tests.independent_planner_provider import IndependentPlannerProvider, WireCaptured
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


def invoke(prefix, command, **options):
    args = [*prefix, command]
    for key, value in options.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    return writing_cli.main(args)


def prepare(dataset, case, directory, monkeypatch, *, contaminated=False):
    repo, project, report, writer, planner = dataset
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    data = json.loads((ROOT / f"evals/editorial-inputs/v1/{case}/shared.json").read_text())
    if contaminated:
        inject_reviewed_leaks(data, case)
    (directory / "shared.json").write_text(json.dumps(data, ensure_ascii=False))
    mapping = directory / "source-map.json"
    mapping.write_text(json.dumps(report["file_refs"]))
    ref = directory / "shared-ref.json"
    assert (
        invoke(prefix, "editorial-import", input=directory / "shared.json", source_map=mapping, out=ref) == 0
    )
    common = dict(
        review_mode="informed-v1",
        run_id=uuid4(),
        case=case,
        author_ref="@" + str(ref),
        writer_profile=json.dumps(writer.model_dump(mode="json")),
        reviewer_profile=json.dumps(writer.model_dump(mode="json")),
        export_dir=directory / "export",
    )
    own = dict(
        common,
        plan_mode="own-plan",
        planner_profile=json.dumps(planner.model_dump(mode="json")),
        plan_reviewer_profile=json.dumps(planner.model_dump(mode="json")),
    )
    return prefix, common, own, mapping, ref


def payload(body):
    return json.loads(body["input"][0]["content"][0]["text"])


def expected_current_story(proposal, *, plan_selection_contract=None):
    """Explicit wire contract, independent of the production projection helpers."""
    plan = proposal["plan"]
    result = dict(
        main_question=plan["main_question"],
        route_summary=plan["route_summary"],
        authority="revisable_authorial_decisions",
        listener_promise=plan["listener_promise"],
        episode_answer=plan["working_answer"],
        ending=deepcopy(plan["ending"]) if "last_moment" in plan["ending"] else None,
        opening={key: plan["opening"][key] for key in ("approach", "question_to_open")},
        duration_strategy=dict(estimated_seconds_range=plan["duration_strategy"]["estimated_seconds_range"]),
        acts=[
            {
                ("suggested_priority" if key == "priority" else key): act[key]
                for key in (
                    "title",
                    "listener_question",
                    "intended_change_in_understanding",
                    "prerequisite_concepts",
                    "transitions",
                    "estimated_seconds_range",
                    "priority",
                )
            }
            for act in plan["acts"]
        ],
        speaker_contributions=[
            dict(
                speaker=host["speaker_id"],
                contribution=host["contribution"],
                interaction_purpose=host["interaction_guidance"],
                speech_register=host["speech_register"],
            )
            for host in plan["host_functions"]
        ],
        unresolved_questions=[
            dict(question=row["question"], why_needed=row["why_needed"])
            for row in proposal["research_requests"]
        ],
    )
    if plan_selection_contract is not None:
        assert plan_selection_contract == "plan-material-selection/1"
        options = plan["duration_strategy"]["optional_material"]
        if options:
            result["material_options"] = dict(
                guidance=(
                    "Conditional extensions or cuts from the plan. Use them when adjusting the "
                    "episode's scope; listing them does not mean every item should be spoken."
                ),
                choices=deepcopy(options),
            )
    return result


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_real_ordinary_wire_and_own_candidate_delivery(own_dataset, case, tmp_path, monkeypatch):
    prefix, common, own, mapping, ref = prepare(own_dataset, case, tmp_path, monkeypatch)
    provider = IndependentPlannerProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    assert invoke(prefix, "run", **own) == 0
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "plan",
        "plan-review",
        "draft",
        "review",
    ]
    for key in ("instructions", "input", "text"):
        assert provider.pre_generation[key] == provider.requests[0][key]
    before = payload(provider.pre_generation)
    assert set(before) == {"output_language", "assignment", "dossier", "material_catalog"}
    assert set(before["dossier"]) == {"knowledge_cards"}
    cards = before["dossier"]["knowledge_cards"]
    assert len(cards) >= (6 if case == "gps-clock" else 12)
    assert all(card["claim"] and card["sources"] for card in cards)
    claims = " ".join(card["claim"] for card in cards).casefold()
    assert all(
        term in claims
        for term in (
            ("псевдодальност", "час", "геометр") if case == "gps-clock" else ("регистрац", "смерт", "1855")
        )
    )
    assert not editorial_leaks(case, provider.pre_generation)
    chosen = provider.plan
    for decision in (chosen["plan"]["working_answer"], chosen["plan"]["ending"]["last_moment"]):
        assert decision not in json.dumps(provider.pre_generation, ensure_ascii=False)
    assert json.loads((tmp_path / "export/plan.json").read_text())["output"] == chosen
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    assert journal.output(journal.invocation(own["run_id"], "plan"))["output"] == chosen
    assert chosen["limitations"]
    assert set(chosen["plan"]["ending"]) == {"last_moment", "why_end_here", "promises_to_close"}
    assert chosen["plan"]["ending"]["last_moment"] and chosen["plan"]["ending"]["why_end_here"]
    assert all(act["limitations_to_voice"] for act in chosen["plan"]["acts"])
    assert chosen["plan"]["duration_strategy"]["assumptions"]
    expected_candidate = deepcopy(chosen)
    expected_candidate.pop("limitations")
    for act in expected_candidate["plan"]["acts"]:
        act.pop("limitations_to_voice")
    assert payload(provider.requests[1])["candidate_plan"] == expected_candidate
    author_input = payload(provider.requests[2])
    expected_fields = {"output_language", "assignment", "story", "support", "duration_context"}
    if "craft_reference" in journal.manifest(own["run_id"]):
        expected_fields.add("craft_reference")
    assert set(author_input) == expected_fields
    story = author_input["story"]
    selection = journal.manifest(own["run_id"])[PLAN_SELECTION_KEY]
    assert selection == PLAN_SELECTION_VERSION
    assert story == expected_current_story(chosen, plan_selection_contract=selection)
    review_input = payload(provider.requests[3])
    assert set(review_input) == {
        "output_language",
        "draft",
        "assignment",
        "editorial_frame",
        "support",
        "evidence",
        "duration_context",
    }
    assert review_input["editorial_frame"] == dict(
        main_question=chosen["plan"]["main_question"],
        route_summary=chosen["plan"]["route_summary"],
        authority="revisable_authorial_decisions",
        listener_promise=chosen["plan"]["listener_promise"],
        episode_answer=chosen["plan"]["working_answer"],
        ending=chosen["plan"]["ending"],
        unresolved_questions=story["unresolved_questions"],
    )
    final = json.loads((tmp_path / "export/final.json").read_text())
    assert final["output"] == provider.script
    assert all(
        row["text"] not in (chosen["plan"]["working_answer"], chosen["plan"]["ending"]["last_moment"])
        for row in final["output"]["utterances"]
    )
    assert len({row["text"] for row in final["output"]["utterances"]}) == len(final["output"]["utterances"])
    fixed_path = ROOT / f"evals/editorial-inputs/v1/{case}/fixed-plan.json"
    fixed_proposal = json.loads(fixed_path.read_text())["plan"]
    fixed = fixed_proposal["plan"]
    assert story["opening"]["approach"] != fixed["opening"]["approach"]
    assert story["episode_answer"] != fixed["working_answer"]
    assert story["ending"] != fixed["ending"]
    # The separately admitted fixed control is allowed to carry its own prepared plot.
    fixed_ref = tmp_path / "fixed-ref.json"
    assert (
        invoke(
            prefix,
            "fixed-editorial-import",
            input=fixed_path,
            source_map=mapping,
            shared_ref="@" + str(ref),
            out=fixed_ref,
        )
        == 0
    )
    control = IndependentPlannerProvider()
    control_run_id = uuid4()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: control)
    assert (
        invoke(
            prefix,
            "run",
            **dict(
                common,
                run_id=control_run_id,
                fixed_plan_input_ref="@" + str(fixed_ref),
                export_dir=tmp_path / "fixed-export",
            ),
        )
        == 0
    )
    assert [body["metadata"]["podgen_stage"] for body in control.requests] == ["draft", "review"]
    control_story = payload(control.requests[0])["story"]
    control_selection = journal.manifest(control_run_id)[PLAN_SELECTION_KEY]
    assert control_selection == PLAN_SELECTION_VERSION
    assert control_story == expected_current_story(fixed_proposal, plan_selection_contract=control_selection)


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_actual_first_wire_detects_previous_editorial_contamination(own_dataset, case, tmp_path, monkeypatch):
    prefix, _, own, _, _ = prepare(own_dataset, case, tmp_path, monkeypatch, contaminated=True)
    provider = IndependentPlannerProvider(capture_only=True)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    with pytest.raises(WireCaptured):
        invoke(prefix, "run", **own)
    assert provider.plan is None and not provider.requests
    kinds = {kind for kind, _ in editorial_leaks(case, provider.pre_generation)}
    assert "preassigned_host_scene" in kinds
    assert "prewritten_thread_route" not in kinds
    assert (
        "prepared_experiment_and_arithmetic" if case == "gps-clock" else "prescribed_story_route"
    ) in kinds
    if case == "gps-clock":
        assert {"prepared_measurement_pair", "prepared_arithmetic_result"} <= kinds
