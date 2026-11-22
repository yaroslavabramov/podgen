"""The real draft and addressed rewrite can hand a precise source question back without fake audio."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import author_material, script_revision
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.evaluation.validation import RoleOutputError
from podgen.writing import author_reply
from podgen.writing.projection_versions import V8, V9
from podgen.writing.roles import WriteOutput
from tests.revision_providers import reply
from tests.stage_pause import pause_before_count
from tests.test_editorial_independence import invoke, prepare
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]
QUESTION = {
    "question": "Какие наблюдения привели врача к этой версии?",
    "why_needed": "Без их описания решение осмотреть воду не связано с предыдущей сценой.",
}


class RequestingProvider(ReadingProvider):
    def __init__(self, stage):
        super().__init__(("revise",))
        self.stage = stage

    def submit(self, body, *, client_request_id):
        if body["metadata"]["podgen_stage"] == self.stage:
            self.requests.append(body)
            assert body["text"]["format"]["schema"]["properties"]["result"]["anyOf"]
            return reply(body, {"material_request": QUESTION})
        return super().submit(body, client_request_id=client_request_id)


@pytest.mark.parametrize("stage", ["draft", "revise-1"])
def test_request_is_an_idempotent_stop_bound_to_actual_script_and_can_restart(
    own_dataset, tmp_path, monkeypatch, stage
):
    if stage == "draft":
        prefix, options, _, mapping, ref = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
        options.pop("review_mode")
        fixed = tmp_path / "fixed-ref.json"
        assert (
            invoke(
                prefix,
                "fixed-editorial-import",
                input=ROOT / "evals/editorial-inputs/v1/snow-pump/fixed-plan.json",
                source_map=mapping,
                shared_ref="@" + str(ref),
                out=fixed,
            )
            == 0
        )
        options["fixed_plan_input_ref"] = "@" + str(fixed)
    else:
        prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    provider = RequestingProvider(stage)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    exported = tmp_path / "export"
    if stage == "revise-1":
        class PublicationInterrupted(Exception):
            pass

        save_status = EvaluationJournal.status

        def interrupt_publication(journal, run_id, status, failure=None):
            if failure == "writer_needs_material":
                raise PublicationInterrupted()
            return save_status(journal, run_id, status, failure)

        with monkeypatch.context() as pause:
            pause.setattr(EvaluationJournal, "status", interrupt_publication)
            with pytest.raises(PublicationInterrupted):
                invoke(prefix, "run", **options)
        paid_calls = len(provider.requests)
        assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=exported) == 2
        assert len(provider.requests) == paid_calls
    else:
        assert invoke(prefix, "run", **options) == 2
    needs = json.loads((exported / "material-request.json").read_text())
    report = json.loads((exported / "run.json").read_text())
    assert report["review_status"] == "needs_material" and report["failure_code"] == "writer_needs_material"
    assert needs["material_request"] == QUESTION
    assert "output" not in needs and not (exported / (stage + ".txt")).exists()
    assert not (exported / (stage + ".json")).exists()
    assert QUESTION["question"] in (exported / "review-result.md").read_text()
    count = len(provider.requests)
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=exported) == 2
    assert len(provider.requests) == count
    if stage == "draft":
        assert needs["last_script_ref"] is None
    else:
        assert needs["last_script_ref"] == json.loads((tmp_path / "script-ref.json").read_text())
        packet = json.loads((tmp_path / "shared.json").read_text())
        packet["dossier"]["knowledge_cards"][0]["context"] += " Новое проверенное пояснение."
        (tmp_path / "augmented.json").write_text(json.dumps(packet, ensure_ascii=False))
        ref = tmp_path / "augmented-ref.json"
        assert (
            invoke(
                prefix,
                "editorial-import",
                input=tmp_path / "augmented.json",
                source_map=tmp_path / "source-map.json",
                out=ref,
            )
            == 0
        )
        finished = ReadingProvider(("keep",))
        monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: finished)
        assert (
            invoke(
                prefix,
                "run",
                **dict(
                    options,
                    run_id=uuid4(),
                    author_ref="@" + str(ref),
                    initial_script_ref=json.dumps(needs["last_script_ref"]),
                    export_dir=tmp_path / "continued",
                ),
            )
            == 0
        )
        assert [r["metadata"]["podgen_stage"] for r in finished.requests] == ["reader-review", "fact-review"]
        assert json.loads((tmp_path / "continued/final.json").read_text())["output"] == json.loads(
            (tmp_path / "script.json").read_text()
        )


def test_changed_reply_form_blocks_resume_before_the_first_writer_request(own_dataset, tmp_path, monkeypatch):
    prefix, _, options, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "plan"):
        invoke(prefix, "run", **options)
    repo, project = own_dataset[:2]
    manifest = EvaluationJournal(repo, project).manifest(options["run_id"])
    wrap = author_reply.wire_schema

    def drift(work, *, speaker_labels=()):
        schema = wrap(work, speaker_labels=speaker_labels)
        schema["description"] = "Incompatible new form."
        return schema

    monkeypatch.setattr(author_reply, "wire_schema", drift)
    with pytest.raises(Conflict, match="author_reply_contract_changed"):
        WritingEvaluation(repo, lambda _: provider).run(WritingRunCommand.model_validate(manifest["command"]))
    assert not provider.requests


def test_bound_forms_and_historical_wire():
    raw = '{"utterances":[{"speaker":"Narrator","text":"Speech"}]}'
    assert author_material.unwrap(raw, "draft", V8) == raw
    assert not author_reply.offered("fact-review", V9)
    with pytest.raises(ValueError):
        author_material.unwrap(raw, "draft", V9)
    with pytest.raises(ValueError):
        author_material.unwrap(
            json.dumps({"result": {"material_request": QUESTION, "utterances": []}}), "draft", V9
        )
    manifest = {
        "role_projection_version": V9,
        "response_schemas": {"draft": WriteOutput.model_json_schema()},
        "revision_response_schemas": script_revision.schemas(),
    }
    manifest["writer_response_contract"] = author_reply.contract_pin(manifest, ("Narrator",))
    author_reply.verify_pin(manifest, ("Narrator",))
    with pytest.raises(Conflict):
        author_reply.verify_pin(manifest, ("New narrator",))
    manifest["revision_response_schemas"]["targeted"]["title"] = "Different"
    with pytest.raises(Conflict):
        author_reply.verify_pin(manifest, ("Narrator",))


def test_invalid_material_request_produces_storable_validation_details():
    raw = json.dumps({"result": {"material_request": {"question": "   ", "why_needed": "reason"}}})
    with pytest.raises(RoleOutputError) as failure:
        author_material.unwrap(raw, "draft", V9)
    assert json.loads(json.dumps(failure.value.details))[0]["path"] == ["material_request", "question"]
