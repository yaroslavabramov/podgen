"""Run, preview, archive and recovery bind patches and composition to immutable versions."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen import writing_cli
from podgen.domain.canonical import digest
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from tests.revision_cases import actual_case
from tests.revision_providers import Actual83Provider, ContinuingProvider
from tests.stage_pause import pause_before_count
from tests.test_current_snow_input import source_file
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


def save_outputs(journal, run_id, directory, stages):
    paths = {}
    for stage in stages:
        output = journal.output(journal.invocation(run_id, stage))["output"]
        paths[stage] = directory / (stage + ".json")
        paths[stage].write_text(json.dumps(output, ensure_ascii=False))
    return paths


def test_real_83_ordinary_run_persists_only_three_edits_and_rechecks_the_complete_result(
    own_dataset, tmp_path, monkeypatch
):
    prefix, options, _, source_map, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    packet = ROOT / "evals/editorial-inputs/current/snow-pump/history/before-material-rebuild-2026-11-10.json"
    mapping = {}
    for document in json.loads(packet.read_text())["documents"]:
        path = tmp_path / (document["key"] + "-ref.json")
        assert (
            invoke(
                prefix,
                "editorial-source-import",
                file=source_file(document),
                source_path=document["source_path"],
                purpose=document["purpose"],
                scope="actual-83-revision",
                out=path,
            )
            == 0
        )
        mapping[document["source_path"]] = json.loads(path.read_text())
    source_map = tmp_path / "current-source-map.json"
    source_map.write_text(json.dumps(mapping))
    shared = tmp_path / "current-ref.json"
    assert invoke(prefix, "editorial-import", input=packet, source_map=source_map, out=shared) == 0
    _, _, script, _ = actual_case()
    source, initial = tmp_path / "source.json", tmp_path / "initial-ref.json"
    source.write_text(json.dumps(script, ensure_ascii=False))
    assert invoke(prefix, "script-import", file=source, out=initial) == 0
    options.update(
        author_ref="@" + str(shared),
        initial_script_ref="@" + str(initial),
        review_mode="independent-readings/1",
    )
    references = ModelReferences.create(EditorialInput.model_validate_json(packet.read_bytes()))
    provider = Actual83Provider(references)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "revise-1"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert manifest["script_revision_contract"] == "addressed-script-revision/1"
    pending = journal.request(journal.invocation(options["run_id"], "revise-1"))
    visible = payload(pending)
    assert visible["revision_operation"]["mode"] == "targeted"
    assert not {"listener_understanding", "duration_note", "important_limits"} & set(
        visible["revision_guidance"][1]
    )
    files = save_outputs(journal, options["run_id"], tmp_path, ("reader-review", "fact-review"))
    preview = tmp_path / "preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(shared),
            stage="revise-1",
            profile=options["writer_profile"],
            draft=source,
            reader_feedback=files["reader-review"],
            fact_feedback=files["fact-review"],
            out=preview,
        )
        == 0
    )
    assert json.loads(preview.read_text())["request"] == pending
    changed = deepcopy(script)
    changed["utterances"][2]["text"] += " Stale source mutation."
    source.write_text(json.dumps(changed, ensure_ascii=False))
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(shared),
            stage="revise-1",
            profile=options["writer_profile"],
            draft=source,
            reader_feedback=files["reader-review"],
            fact_feedback=files["fact-review"],
            out=preview,
        )
        == 2
    )

    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    record = journal.output(journal.invocation(options["run_id"], "revise-1"))
    assert set(record["output"]) == {"utterances"}
    assert len(record["revision"]["wire_output"]["edits"]) == 3
    for index, (before, after) in enumerate(
        zip(script["utterances"], record["output"]["utterances"], strict=True)
    ):
        if index not in (0, 4, 11):
            assert before == after
    readers = [
        payload(body)
        for body in provider.requests
        if body["metadata"]["podgen_stage"] in ("reader-review-1", "fact-review-1")
    ]
    assert len(readers) == 2
    assert all(
        [r["text"] for r in reading["script"]["utterances"]]
        == [r["text"] for r in record["output"]["utterances"]]
        for reading in readers
    )
    context = json.loads((tmp_path / "export/revise-1-revision-context.json").read_text())
    assert context["script_hash"] == digest(script)
    assert context["revision_operation"]["targets"][0]["start"] >= 0
    assert (tmp_path / "export/revise-1-application.json").exists()
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "revise-1") == pending
    )
    assert journal.manifest(options["run_id"]) == manifest


@pytest.mark.parametrize("first_mode", ["sequence", "structural", "fact-structural"])
def test_next_local_revision_uses_the_selected_composition_without_losing_open_research(
    own_dataset, tmp_path, monkeypatch, first_mode
):
    prefix, _, options, _, shared = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options["review_mode"] = "independent-readings/1"
    provider = ContinuingProvider(first_mode)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "revise-2"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    first = journal.output(journal.invocation(options["run_id"], "revise-1"))
    original = journal.output(journal.invocation(options["run_id"], "draft"))["output"]
    plan = journal.output(journal.invocation(options["run_id"], "plan"))["output"]
    assert first["composition"]["main_question"] == "Chosen question"
    assert first["composition"]["unresolved_questions"] == [
        dict(question="Still open?", why_needed="Reordering alone does not resolve research.")
    ]
    assert first["output"]["utterances"][0] == original["utterances"][0]
    assert first["revision"]["operation"]["mode"] == (
        "sequence" if first_mode == "sequence" else "structural"
    )
    second = journal.request(journal.invocation(options["run_id"], "revise-2"))
    visible = payload(second)
    assert visible["editorial_frame"]["main_question"] == "Chosen question"
    assert visible["editorial_frame"]["main_question"] != plan["plan"]["main_question"]
    assert visible["editorial_frame"]["unresolved_questions"] == first["composition"]["unresolved_questions"]
    assert visible["revision_operation"]["mode"] == "targeted"
    assert visible["revision_operation"]["script_version"] == 1
    assert not any(t.get("utterance_index") == 0 for t in visible["revision_operation"]["targets"])
    assert any(t.get("utterance_index") == 3 for t in visible["revision_operation"]["targets"])
    assert visible["revision_guidance"][1]["revision_scope"]["dependent_passages"]
    paths = save_outputs(
        journal, options["run_id"], tmp_path, ("revise-1", "reader-review-1", "fact-review-1", "plan")
    )
    preview = tmp_path / "next-preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(shared),
            stage="revise-2",
            profile=options["writer_profile"],
            draft=paths["revise-1"],
            reader_feedback=paths["reader-review-1"],
            fact_feedback=paths["fact-review-1"],
            approved_plan=paths["plan"],
            out=preview,
        )
        == 0
    )
    assert json.loads(preview.read_text())["request"] == second

    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    final = journal.output(journal.invocation(options["run_id"], "revise-2"))
    assert final["composition"] == first["composition"]
    assert set(final["output"]) == {"utterances"}
    assert journal.output(journal.invocation(options["run_id"], "plan"))["output"] == plan
    assert [b["metadata"]["podgen_stage"] for b in provider.requests].count("revise-1") == 1
    assert [b["metadata"]["podgen_stage"] for b in provider.requests].count("revise-2") == 1
    assert journal.manifest(options["run_id"]) == manifest
