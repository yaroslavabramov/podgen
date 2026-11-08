"""Ordinary CLI, actual Snow script, and local provider doubles: never external inference."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.evaluation.reading_workflow import VERSION
from podgen.runtime.models.contracts import WireReply
from podgen.writing.prompts import load_reading_instructions
from tests.author_responses import author_response
from tests.independent_planner_provider import IndependentPlannerProvider
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


class Paused(Exception):
    pass


class ReadingProvider:
    def __init__(self, decisions=("keep",), *, invalid=None, pause=None, fact_need=False):
        self.decisions = decisions
        self.invalid, self.pause, self.fact_need = invalid, pause, fact_need
        self.requests, self.counts = [], []
        self.upstream = IndependentPlannerProvider()

    def count(self, body):
        if self.pause and body["text"]["format"]["name"] == self.pause.replace("-", "_"):
            raise Paused()
        self.counts.append(deepcopy(body))
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        stage = body["metadata"]["podgen_stage"]
        data = payload(body)
        if stage.startswith("plan") or stage == "draft":
            return self.upstream.submit(body, client_request_id=client_request_id)
        if body["text"]["format"]["name"] == "anchor_choices":
            result = dict(
                choices=[
                    dict(anchor_id=a["anchor_id"], selector_id=a["selector_ids"][0])
                    for a in data["invalid_anchors"]
                ]
            )
        elif stage.startswith("revise"):
            result = dict(
                utterances=[
                    {k: v for k, v in row.items() if k != "index"} for row in data["script"]["utterances"]
                ]
            )
            result["utterances"][0]["text"] += " Исправленная тестовая связка."
            operation = data.get("revision_operation")
            if operation:
                frame = data.get("editorial_frame", {})
                composition = dict(
                    main_question=frame.get("main_question", "Тестовый вопрос."),
                    listener_promise=frame.get("listener_promise", "Тестовое обещание."),
                    route_summary=frame.get("route_summary", "Тестовый маршрут."),
                    decisions=["Синтетическое решение о композиции."],
                )
                if operation["mode"] == "targeted":
                    target = next(t for t in operation["targets"] if "text" in t)
                    result = dict(
                        edits=[
                            dict(
                                operation="replace",
                                target_id=target["target_id"],
                                text=target["text"] + " Исправленная тестовая связка.",
                            )
                        ],
                        composition_update=None,
                    )
                elif operation["mode"] == "sequence":
                    selected = operation["sequences"][0]
                    rows = [
                        dict(speaker=r["speaker"], text=r["text"])
                        for r in data["script"]["utterances"][
                            selected["first_utterance_index"] : selected["last_utterance_index"] + 1
                        ]
                    ]
                    rows[0]["text"] += " Исправленная тестовая последовательность."
                    result = dict(
                        sequences=[dict(range_id=selected["range_id"], utterances=rows)],
                        edits=[],
                        composition=composition,
                    )
                else:
                    result = dict(script=result, composition=composition)
        else:
            index = int(stage.rsplit("-", 1)[1]) if stage[-1].isdigit() else 0
            decision = self.decisions[min(index, len(self.decisions) - 1)]
            reader = stage.startswith("reader")
            quote = data["script"]["utterances"][0]["text"]
            anchor = dict(utterance_index=999 if stage == self.invalid else 0, quote=quote)
            finding = dict(
                dimension="transition" if reader else "factuality",
                priority="important",
                anchors=[anchor],
                observation="Сохранённое замечание читателя."
                if reader
                else "Сохранённая фактическая оговорка.",
                requested_change="Исправить связь." if reader else "Сохранить точный предел вывода.",
                preserve=["Существенное ограничение."],
            )
            if not reader:
                finding["support_labels"] = [data["support"][-1].get("card_id", data["support"][-1]["label"])]
            need = self.fact_need and not reader
            result = dict(
                reading_mode="script_only" if reader else "informed",
                listener_answer="COLD_REVIEW_SECRET" if reader else "FACT_REVIEW_SECRET",
                important_limits=["Полная существенная оговорка."],
                strengths=[
                    dict(
                        anchors=[dict(utterance_index=0, quote="Non-exact quoted wording")],
                        reason="Сохраняем сильное место.",
                    )
                ]
                if stage == self.invalid
                else [],
                findings=[finding] if decision == "revise" or stage == self.invalid else [],
                research_requests=[
                    dict(
                        anchors=[anchor],
                        question="Каково основание?",
                        why_needed="Без него невозможно закончить ответ.",
                    )
                ]
                if need
                else [],
                duration_note="Длительность не измерена.",
                recommendation="needs_material" if need else decision,
            )
        if (
            stage.startswith(("reader-review", "fact-review"))
            and "revision_scope" in body["text"]["format"]["schema"]["properties"]
        ):
            if self.invalid == stage:
                result["recommendation"] = "revise"
            result["revision_scope"] = dict(
                scale="local" if result["recommendation"] == "revise" else "none",
                reason="Синтетическое редакционное решение для теста; не измерение качества.",
                anchors=[dict(utterance_index=0, quote=quote)]
                if result["recommendation"] == "revise"
                else [],
            )
            if any(
                "dependent_passages" in definition.get("properties", {})
                for definition in body["text"]["format"]["schema"].get("$defs", {}).values()
            ):
                result["revision_scope"]["dependent_passages"] = []
        return WireReply(
            json.dumps(
                dict(
                    id="resp_" + stage,
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(
                            type="message",
                            role="assistant",
                            content=[dict(type="output_text", text=author_response(body, result))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )


def setup_review(dataset, directory, monkeypatch):
    prefix, common, _, _, _ = prepare(dataset, "snow-pump", directory, monkeypatch)
    common.pop("review_mode")  # Exercise the ordinary default, not a special review harness.
    script = json.loads(
        (ROOT / "evals/reports/2026-11-05-planning/own-plan/snow-pump/draft.json").read_text()
    )["output"]
    source, ref = directory / "script.json", directory / "script-ref.json"
    source.write_text(json.dumps(script, ensure_ascii=False))
    assert invoke(prefix, "script-import", file=source, out=ref) == 0
    common["initial_script_ref"] = "@" + str(ref)
    return prefix, common, script


def test_default_saved_snow_two_isolated_readings_and_repeated_final(own_dataset, tmp_path, monkeypatch):
    prefix, options, script = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = ReadingProvider(("revise", "keep"))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 0
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "reader-review",
        "fact-review",
        "revise-1",
        "reader-review-1",
        "fact-review-1",
    ]
    first, fact, rewrite, final_read, final_fact = map(payload, provider.requests)
    assert set(first) == {"script", "audience", "format", "genre", "output_language", "duration_context"}
    assert first["audience"] == "General listeners; no prior topic knowledge is assumed."
    assert first["genre"] == "podcast"
    assert [
        {k: v for k, v in row.items() if k != "index"} for row in first["script"]["utterances"]
    ] == script["utterances"]
    assert first["script"] == fact["script"]
    assert final_read["script"] == final_fact["script"] != first["script"]
    assert "COLD_REVIEW_SECRET" not in json.dumps(fact)
    assert "COLD_REVIEW_SECRET" not in json.dumps(final_fact)
    assert "FACT_REVIEW_SECRET" not in json.dumps(final_read)
    assert set(fact["factual_remit"]) >= {"question", "required_topics", "knowledge_cutoff"}
    assert not ({"assignment", "story", "plan", "readings", "revision_guidance"} & set(fact))
    assert "evidence" not in rewrite
    assert 0 < len(rewrite["repair_evidence"]) < len(fact["evidence"])
    assert all(
        set(row["supports"]) & set(rewrite["revision_guidance"][1]["findings"][0]["support_labels"])
        for row in rewrite["repair_evidence"]
    )
    assert [r["origin"] for r in rewrite["revision_guidance"]] == ["reader", "fact"]
    assert all("important_limits" not in guidance for guidance in rewrite["revision_guidance"])
    assert rewrite["revision_guidance"][1]["findings"][0]["preserve"] == ["Существенное ограничение."]
    assert len(rewrite["support"]) == 1  # No plan: only the specific factual repair's support.
    selected = rewrite["revision_guidance"][1]["findings"][0]["support_labels"][0]
    source = next(card for card in fact["support"] if card["label"] == selected)
    assert rewrite["support"][0] == {key: source[key] for key in ("claim", "context", "sources")}
    assert "source_excerpts" not in rewrite["support"][0]  # No explicit narrative selection in this old packet.
    assert provider.requests[0]["instructions"] == load_reading_instructions("reader-review").body
    out = tmp_path / "export"
    assert (out / "final.txt").exists() and "Фактологическая проверка · версия 2" in (
        out / "review-result.md"
    ).read_text()
    outcome = json.loads((out / "review-outcome.json").read_text())
    assert outcome["status"] == "keep" and len(outcome["rounds"]) == 2
    count = len(provider.requests)
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=out) == 0
    assert len(provider.requests) == count


@pytest.mark.parametrize("need,expected", [(True, "needs_material"), (False, "revision_limit_exhausted")])
def test_terminal_refusal_keeps_both_readings_and_exact_candidate(
    own_dataset, tmp_path, monkeypatch, need, expected
):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    options["max_script_revisions"] = 0
    provider = ReadingProvider(("revise",), fact_need=need)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 2
    out = tmp_path / "export"
    outcome = json.loads((out / "review-outcome.json").read_text())
    assert outcome["status"] == expected and len(provider.requests) == 2
    assert (out / "candidate-final.txt").exists() and not (out / "final.txt").exists()
    assert len(outcome["reading_refs"]) == 2
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=out) == 2
    assert len(provider.requests) == 2


@pytest.mark.parametrize(
    "invalid",
    ["reader-review", "fact-review", "reader-review-1", "fact-review-1", "reader-review-2", "fact-review-2"],
)
def test_each_role_and_version_has_one_bounded_anchor_repair(own_dataset, tmp_path, monkeypatch, invalid):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    last = 2 if invalid.endswith("-2") else 1
    options.update(max_script_revisions=last, review_anchor_policy="bounded-selection-v1")
    provider = ReadingProvider((*("revise" for _ in range(last)), "keep"), invalid=invalid)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == (2 if invalid.endswith(f"-{last}") else 0)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    attempts = journal.attempts(options["run_id"], invalid)
    assert [a["attempt_kind"] for a in attempts] == ["role", "anchor_repair"]
    repaired = journal.output(attempts[-1])["output"]
    assert repaired["findings"][0]["observation"].startswith("Сохранённ")
    if invalid.startswith("fact"):
        assert repaired["findings"][0]["support_labels"]
    assert read_outcome(journal, options["run_id"]) is not None


def test_pending_final_reading_resumes_pinned_request_without_repeating_earlier_reads(
    own_dataset, tmp_path, monkeypatch
):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = ReadingProvider(("revise", "keep"), pause="fact-review-1")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    pending = journal.request(journal.invocation(options["run_id"], "fact-review-1"))
    provider.pause = None
    monkeypatch.setattr(
        "podgen.writing.prompts.load_reading_instructions",
        lambda *a: (_ for _ in ()).throw(AssertionError("Must use pinned instructions")),
    )
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert provider.requests[-1] == pending
    assert len(provider.requests) == 5


def test_ordinary_own_plan_default_runs_both_readings(own_dataset, tmp_path, monkeypatch):
    prefix, _, own, _, _ = prepare(own_dataset, "gps-clock", tmp_path, monkeypatch)
    own.pop("review_mode")
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **own) == 0
    assert [r["metadata"]["podgen_stage"] for r in provider.requests] == [
        "plan",
        "plan-review",
        "draft",
        "reader-review",
        "fact-review",
    ]


def test_direct_command_default_is_independent_and_old_wire_restores_informed(own_dataset):
    repo, project, report, writer, _ = own_dataset
    fields = dict(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="snow-pump",
        author_ref=RuntimeRef.model_validate(report["author_inputs"]["snow-pump"]),
        writer_profile_ref=writer,
        reviewer_profile_ref=writer,
    )
    assert WritingRunCommand(**fields).review_mode == VERSION
    assert (
        WritingRunCommand(**fields, input_contract="legacy-author-materials/1").review_mode == "informed-v1"
    )


def test_explicit_new_listener_metadata_never_falls_back_to_full_brief(own_dataset, tmp_path, monkeypatch):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    data = json.loads((tmp_path / "shared.json").read_text())
    data["assignment"].update(
        key="new-arbitrary-episode",
        audience="FULL_BRIEF_SECRET: колонка на Брод-стрит",
        style_guidance=["STYLE_SECRET: prepared opening and final answer"],
        listener_profile="Взрослые слушатели без специальной подготовки.",
        genre="Документальное расследование",
    )
    source, ref = tmp_path / "new-shared.json", tmp_path / "new-shared-ref.json"
    source.write_text(json.dumps(data, ensure_ascii=False))
    assert (
        invoke(prefix, "editorial-import", input=source, source_map=tmp_path / "source-map.json", out=ref)
        == 0
    )
    options.update(case="new-arbitrary-episode", author_ref="@" + str(ref))
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 0
    cold = payload(provider.requests[0])
    assert cold["audience"] == data["assignment"]["listener_profile"]
    assert cold["genre"] == data["assignment"]["genre"]
    assert "FULL_BRIEF_SECRET" not in json.dumps(cold)
    assert "STYLE_SECRET" not in json.dumps(cold)


def test_script_import_exact_bytes_idempotent_and_different_json_preserved(
    own_dataset, tmp_path, monkeypatch
):
    prefix, _, script = setup_review(own_dataset, tmp_path, monkeypatch)
    before = json.loads((tmp_path / "script-ref.json").read_text())
    assert invoke(prefix, "script-import", file=tmp_path / "script.json", out=tmp_path / "again.json") == 0
    assert json.loads((tmp_path / "again.json").read_text()) == before
    other = tmp_path / "formatted-script.json"
    other.write_text(json.dumps(script, ensure_ascii=False, indent=2))
    assert invoke(prefix, "script-import", file=other, out=tmp_path / "other-ref.json") == 0
    after = json.loads((tmp_path / "other-ref.json").read_text())
    assert before != after
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    assert (
        journal.read(RuntimeRef.model_validate(before))["script_hash"]
        == journal.read(RuntimeRef.model_validate(after))["script_hash"]
    )


def test_preview_matches_ordinary_reading_wire(own_dataset, tmp_path, monkeypatch):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 0
    for stage in ("reader-review", "fact-review", "reader-review-2", "fact-review-2"):
        target = tmp_path / (stage + "-preview.json")
        assert (
            invoke(
                prefix,
                "preview",
                shared_ref=options["author_ref"],
                case="snow-pump",
                stage=stage,
                profile=options["reviewer_profile"],
                run_id=options["run_id"],
                draft=tmp_path / "script.json",
                out=target,
            )
            == 0
        )
        preview = json.loads(target.read_text())
        base = stage.removesuffix("-2")
        request = deepcopy(next(r for r in provider.requests if r["metadata"]["podgen_stage"] == base))
        if stage != base:
            from uuid import uuid5

            request["metadata"].update(
                podgen_stage=stage, podgen_invocation_id=str(uuid5(options["run_id"], "invocation:" + stage))
            )
            request["text"]["format"]["name"] = stage.replace("-", "_")
        assert preview["request"] == request and preview["endpoint_called"] is False


def test_two_rewrites_exhaust_limit_only_after_both_final_readings(own_dataset, tmp_path, monkeypatch):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = ReadingProvider(("revise",))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 2
    stages = [r["metadata"]["podgen_stage"] for r in provider.requests]
    assert stages == [
        "reader-review",
        "fact-review",
        "revise-1",
        "reader-review-1",
        "fact-review-1",
        "revise-2",
        "reader-review-2",
        "fact-review-2",
    ]
    outcome = json.loads((tmp_path / "export/review-outcome.json").read_text())
    assert outcome["status"] == "revision_limit_exhausted" and len(outcome["rounds"]) == 3


def test_strict_failed_anchor_can_resume_with_explicit_address_recovery(own_dataset, tmp_path, monkeypatch):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    options["max_script_revisions"] = 0
    provider = ReadingProvider(invalid="reader-review")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 2
    assert len(provider.requests) == 1
    assert (
        writing_cli.main(
            [
                *prefix,
                "resume",
                "--run-id",
                str(options["run_id"]),
                "--export-dir",
                str(tmp_path / "export"),
                "--repair-review-anchors",
            ]
        )
        == 2
    )
    assert len(provider.requests) == 3
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    assert journal.invocation(options["run_id"], "reader-review")["status"] == "completed"
    assert read_outcome(journal, options["run_id"])["status"] == "revision_limit_exhausted"


def test_saved_reading_cannot_be_rebound_to_changed_spoken_words(own_dataset, tmp_path, monkeypatch):
    from podgen.domain.errors import Conflict
    from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
    from podgen.runtime.evaluation.reading_stage import execute_reading_stage

    prefix, options, script = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = ReadingProvider(pause="fact-review")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    repo, project = own_dataset[:2]
    journal = EvaluationJournal(repo, project)
    manifest = journal.manifest(options["run_id"])
    command = WritingRunCommand.model_validate(manifest["command"])
    generation = generation_repo(repo)
    materials = load_editorial_materials(generation, project, command.author_ref, case_id="snow-pump")
    changed = deepcopy(script)
    changed["utterances"][0]["text"] += " Другие произносимые слова."
    with pytest.raises(Conflict, match="independent_reading_binding_mismatch"):
        execute_reading_stage(
            "reader-review",
            journal=journal,
            gateway=None,
            generation=generation,
            command=command,
            manifest=manifest,
            materials=materials,
            totals=dict(input=0, output=0),
            guard=lambda: None,
            script=changed,
            script_ref=command.initial_script_ref.model_dump(mode="json"),
        )
    assert len(provider.requests) == 1
