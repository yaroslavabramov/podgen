"""Real inputs through ordinary CLI; reviewed semantic boundary, not a universal classifier."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
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
    assert set(before["dossier"]) == {"summary", "throughline_constraints", "threads", "knowledge_cards"}
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
    assert chosen["plan"]["ending"]["answer_to_deliver"] not in json.dumps(
        provider.pre_generation, ensure_ascii=False
    )
    assert json.loads((tmp_path / "export/plan.json").read_text())["output"] == chosen
    assert payload(provider.requests[1])["candidate_plan"] == chosen
    story = payload(provider.requests[2])["story"]
    assert story["opening"]["approach"] == chosen["plan"]["opening"]["approach"]
    assert story["conclusion"]["meaning"] == chosen["plan"]["ending"]["answer_to_deliver"]
    assert story["duration_strategy"] == chosen["plan"]["duration_strategy"]
    for actual, act in zip(story["acts"], chosen["plan"]["acts"], strict=True):
        assert all(value == act[key] for key, value in actual.items())
    assert story["speaker_contributions"] == [
        {
            (
                "speaker"
                if key == "speaker_id"
                else "interaction_purpose"
                if key == "interaction_guidance"
                else key
            ): value
            for key, value in host.items()
        }
        for host in chosen["plan"]["host_functions"]
    ]
    final = json.loads((tmp_path / "export/final.json").read_text())
    assert final["output"] == provider.script
    assert all(
        row["text"] != chosen["plan"]["ending"]["answer_to_deliver"] for row in final["output"]["utterances"]
    )
    assert len({row["text"] for row in final["output"]["utterances"]}) == len(final["output"]["utterances"])
    fixed_path = ROOT / f"evals/editorial-inputs/v1/{case}/fixed-plan.json"
    fixed = json.loads(fixed_path.read_text())["plan"]["plan"]
    assert story["opening"]["approach"] != fixed["opening"]["approach"]
    assert story["conclusion"]["meaning"] != fixed["ending"]["answer_to_deliver"]
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
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: control)
    assert (
        invoke(
            prefix,
            "run",
            **dict(
                common,
                run_id=uuid4(),
                fixed_plan_input_ref="@" + str(fixed_ref),
                export_dir=tmp_path / "fixed-export",
            ),
        )
        == 0
    )
    assert [body["metadata"]["podgen_stage"] for body in control.requests] == ["draft", "review"]
    control_story = payload(control.requests[0])["story"]
    assert control_story["opening"]["approach"] == fixed["opening"]["approach"]
    assert [act["title"] for act in control_story["acts"]] == [act["title"] for act in fixed["acts"]]


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_actual_first_wire_detects_previous_editorial_contamination(own_dataset, case, tmp_path, monkeypatch):
    prefix, _, own, _, _ = prepare(own_dataset, case, tmp_path, monkeypatch, contaminated=True)
    provider = IndependentPlannerProvider(capture_only=True)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    with pytest.raises(WireCaptured):
        invoke(prefix, "run", **own)
    assert provider.plan is None and not provider.requests
    kinds = {kind for kind, _ in editorial_leaks(case, provider.pre_generation)}
    assert {"preassigned_host_scene", "prewritten_thread_route"} <= kinds
    assert (
        "prepared_experiment_and_arithmetic" if case == "gps-clock" else "prescribed_story_route"
    ) in kinds
    if case == "gps-clock":
        assert {"prepared_measurement_pair", "prepared_arithmetic_result"} <= kinds
