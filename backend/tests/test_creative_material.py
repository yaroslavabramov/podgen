"""Creative selection preserves facts; editorial evidence remains available separately."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing.creative_material import creative_card_view, creative_dossier_view
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.plan_selection import KEY as PLAN_SELECTION_KEY
from podgen.writing.plan_selection import VERSION as PLAN_SELECTION_VERSION
from podgen.writing.projections import evidence_view, knowledge_view
from tests.test_current_snow_input import source_file
from tests.test_editorial_independence import expected_current_story, invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_real_cards_keep_authored_story_facts_and_complete_checker_material(case):
    data = json.loads((ROOT / f"evals/editorial-inputs/current/{case}/shared.json").read_text())
    packet = EditorialInput.model_validate(data)
    before = packet.model_dump(mode="json")
    documents = {doc.key: doc.title for doc in packet.documents}
    cards = [
        creative_card_view(card, sources=[documents[excerpt.document] for excerpt in card.excerpts])
        for card in packet.dossier.knowledge_cards
    ]
    assert creative_dossier_view(cards) == {"knowledge_cards": cards}
    for view, card in zip(cards, packet.dossier.knowledge_cards, strict=True):
        assert view == dict(
            claim=card.story_facts,
            sources=list(dict.fromkeys(documents[excerpt.document] for excerpt in card.excerpts)),
        )
    checker = knowledge_view(packet, labels=True)
    for view, card in zip(checker, packet.dossier.knowledge_cards, strict=True):
        assert view["title"] == card.title
        assert view["claim"] == card.claim
        assert view["context"] == card.context
        assert view["limitations"] == card.limitations
    assert all(row["source"] and row["text"] for row in evidence_view(packet))
    assert packet.model_dump(mode="json") == before


@pytest.mark.parametrize("authored", [False, True])
def test_creative_card_never_filters_words_shortens_meaning_or_mutates_sources(authored):
    from types import SimpleNamespace

    card = SimpleNamespace(
        claim="  The notice instructed residents not to enter.\n" * 1000,
        context="The account was published later; the date of learning is unknown.\n" * 1000,
        limitations=["A separate editorial check."],
    )
    sources = ["Attributed source", "Attributed source", "Another account"]
    if authored:
        card.story_facts = "  The posted instruction told residents not to enter.\n" * 1000
    original = deepcopy(vars(card))
    view = creative_card_view(card, sources=sources)
    expected = dict(sources=["Attributed source", "Another account"])
    if authored:
        expected["claim"] = card.story_facts
    else:
        expected.update(claim=card.claim, context=card.context)
    assert view == expected
    assert vars(card) == original
    assert sources == ["Attributed source", "Attributed source", "Another account"]


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_ordinary_cli_compiles_creative_and_checker_inputs_from_current_packets(
    repo, scope, case, tmp_path, monkeypatch, capsys
):
    from podgen.domain.contracts.common import RuntimeRef
    from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
    from podgen.runtime.evaluation.profiles import create_profile
    from tests.own_plan_provider import proposal

    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    project, _ = scope
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    packet_path = ROOT / f"evals/editorial-inputs/current/{case}/shared.json"
    data = json.loads(packet_path.read_text())
    mapping = {}
    for document in data["documents"]:
        ref_path = tmp_path / (document["key"] + ".json")
        result = invoke(
            prefix,
            "editorial-source-import",
            file=source_file(document),
            source_path=document["source_path"],
            purpose=document["purpose"],
            scope="creative-material-preview",
            out=ref_path,
        )
        captured = capsys.readouterr()
        assert result == 0, captured.err
        mapping[document["source_path"]] = json.loads(ref_path.read_text())
    source_map = tmp_path / "source-map.json"
    source_map.write_text(json.dumps(mapping))
    shared_ref = tmp_path / "shared-ref.json"
    assert invoke(prefix, "editorial-import", input=packet_path, source_map=source_map, out=shared_ref) == 0
    materials = load_editorial_materials(
        generation_repo(repo), project, RuntimeRef.model_validate_json(shared_ref.read_bytes()), case_id=case
    )
    assert materials.packet.model_dump(mode="json") == data
    profile = create_profile(
        repo,
        project,
        model="offline-only",
        context_limit=1050000,
        output_limit=32768,
        roles=["planner", "plan_reviewer", "writer", "editorial_reviewer"],
    )
    profile_ref = json.dumps(profile.ref.model_dump(mode="json"))
    selected = {materials.material_catalog[1], materials.material_catalog[-1]}
    plan = proposal(
        dict(material_catalog=materials.material_catalog, speaker_labels=materials.speaker_labels), "offline"
    )
    plan["plan"]["opening"]["material_refs"] = sorted(selected)
    plan["plan"]["acts"][0]["material_refs"] = sorted(selected)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(plan, ensure_ascii=False))
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            dict(
                utterances=[
                    dict(speaker=speaker, text=f"Offline transmission check {index}.")
                    for index, speaker in enumerate(materials.speaker_labels)
                ]
            )
        )
    )
    inputs = {
        "plan": {},
        "plan-review": dict(candidate=candidate),
        "draft": dict(approved_plan=candidate),
        "fact-review": dict(draft=script),
    }
    requests = {}
    for stage, options in inputs.items():
        out = tmp_path / (stage + ".json")
        result = invoke(
            prefix,
            "preview",
            shared_ref="@" + str(shared_ref),
            case=case,
            stage=stage,
            profile=profile_ref,
            run_id=uuid4(),
            out=out,
            **options,
        )
        captured = capsys.readouterr()
        assert result == 0, captured.err
        compiled = json.loads(out.read_text())
        assert compiled["endpoint_called"] is False
        requests[stage] = payload(compiled["request"])
    cards = data["dossier"]["knowledge_cards"]
    planner = requests["plan"]["dossier"]
    assert set(planner) == {"knowledge_cards"}
    assert [card["claim"] for card in planner["knowledge_cards"]] == [card["story_facts"] for card in cards]
    assert all(
        set(card) <= {"claim", "sources", "source_excerpts", "card_id"} for card in planner["knowledge_cards"]
    )
    assert [card["claim"] for card in requests["draft"]["support"]] == [
        card["story_facts"] for card in cards if card["card_id"] in selected
    ]
    assert all(set(card) <= {"claim", "sources", "source_excerpts"} for card in requests["draft"]["support"])
    assert requests["plan-review"]["dossier"] == planner
    assert "evidence" not in requests["plan-review"]
    for view, original in zip(planner["knowledge_cards"], cards, strict=True):
        assert bool(view.get("source_excerpts")) == bool(original.get("narrative_excerpt_indices"))
    for stage in ("fact-review",):
        support = (
            requests[stage]["dossier"]["knowledge_cards"]
            if stage == "plan-review"
            else requests[stage]["support"]
        )
        for view, source in zip(support, cards, strict=True):
            assert view["claim"] == source["claim"]
            assert view["context"] == source["context"]
            assert view["limitations"] == source["limitations"]
            assert view["title"] == source["title"]
        assert requests[stage]["evidence"]


def test_current_gps_fixed_import_and_run_bind_the_current_snapshot(
    own_dataset, tmp_path, monkeypatch, capsys
):
    from podgen import writing_cli

    prefix, common, _, source_map, _ = prepare(own_dataset, "gps-clock", tmp_path, monkeypatch)
    folder = ROOT / "evals/editorial-inputs/current/gps-clock"
    shared_ref = tmp_path / "current-shared-ref.json"
    assert (
        invoke(
            prefix, "editorial-import", input=folder / "shared.json", source_map=source_map, out=shared_ref
        )
        == 0
    )
    current_fixed = json.loads((folder / "fixed-plan.json").read_text())
    migration = json.loads((folder / "card-id-migration.json").read_text())
    stale = deepcopy(current_fixed)
    stale["shared_input_digest"] = migration["story_facts_revision"]["previous_digest"]
    stale_path = tmp_path / "stale-fixed.json"
    stale_path.write_text(json.dumps(stale, ensure_ascii=False))
    assert (
        invoke(
            prefix,
            "fixed-editorial-import",
            input=stale_path,
            source_map=source_map,
            shared_ref="@" + str(shared_ref),
            out=tmp_path / "stale-ref.json",
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["code"] == "fixed_editorial_input_binding_mismatch"
    fixed_ref = tmp_path / "current-fixed-ref.json"
    assert (
        invoke(
            prefix,
            "fixed-editorial-import",
            input=folder / "fixed-plan.json",
            source_map=source_map,
            shared_ref="@" + str(shared_ref),
            out=fixed_ref,
        )
        == 0
    )
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    options = dict(
        common,
        review_mode="independent-readings/1",
        run_id=uuid4(),
        author_ref="@" + str(shared_ref),
        fixed_plan_input_ref="@" + str(fixed_ref),
        export_dir=tmp_path / "fixed-run",
    )
    assert invoke(prefix, "run", **options) == 0
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "draft",
        "reader-review",
        "fact-review",
    ]
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    selection = journal.manifest(options["run_id"])[PLAN_SELECTION_KEY]
    assert selection == PLAN_SELECTION_VERSION
    historical_story = expected_current_story(current_fixed["plan"])
    assert "material_options" not in historical_story
    expected = expected_current_story(current_fixed["plan"], plan_selection_contract=selection)
    assert expected == dict(
        historical_story,
        material_options=dict(
            guidance=(
                "Conditional extensions or cuts from the plan. Use them when adjusting the "
                "episode's scope; listing them does not mean every item should be spoken."
            ),
            choices=[
                "Краткое исключение с заранее известной высотой, если оно помогает снять реальное возражение."
            ],
        ),
    )
    assert payload(provider.requests[0])["story"] == expected
    assert expected["episode_answer"] == (
        "Приёмник совместно оценивает положение и одну общую часовую поправку по псевдодальностям."
    )
    assert json.loads((tmp_path / "fixed-run/final.json").read_text())["output"] == provider.upstream.script
