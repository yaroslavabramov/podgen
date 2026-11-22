"""A native short episode hands the reader's whole-episode choice to the real rewriter."""

import json

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.contracts import WireReply
from podgen.writing.reader_schema import VERSION
from tests.author_responses import author_response, read_work
from tests.stage_pause import pause_before_count
from tests.test_craft_reference import reference_resource
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_script_handoff_runtime import EditingProvider


class WholeEpisodeProvider(EditingProvider):
    def __init__(self, *, bad_scope=False):
        super().__init__()
        self.bad_scope = bad_scope

    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == "draft":
            envelope = json.loads(reply.body)
            content = envelope["output"][0]["content"][0]
            script = read_work(body, content["text"])
            for index, row in enumerate(script["utterances"]):
                row["text"] = f"Эпизод {index}. " + row["text"]
            self.original = script
            content["text"] = author_response(body, script)
            reply = WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        if (
            body["metadata"]["podgen_stage"] == "reader-review"
            and body["text"]["format"]["name"] != "anchor_choices"
        ):
            envelope = json.loads(reply.body)
            content = envelope["output"][0]["content"][0]
            review = json.loads(content["text"])
            review["revision_scope"].update(
                scale="whole_episode",
                reason="Синтетическое решение: заново выбрать порядок открытия и развязки всего рассказа.",
                anchors=review["findings"][0]["anchors"],
            )
            if self.bad_scope:
                review["revision_scope"]["anchors"] = [
                    dict(
                        utterance_index=999,
                        quote=review["revision_scope"]["anchors"][0]["quote"],
                    )
                ]
            content["text"] = json.dumps(review, ensure_ascii=False)
            reply = WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        return reply


@pytest.mark.parametrize("repair", [False, True], ids=["exact", "selector-repair"])
def test_native_whole_episode_scope_survives_preview_persistence_and_resume(
    own_dataset, tmp_path, monkeypatch, repair
):
    reference_resource(tmp_path, monkeypatch)
    prefix, _, options, _, shared_ref = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options["review_mode"] = "independent-readings/1"
    provider = WholeEpisodeProvider(bad_scope=repair)
    if repair:
        options["review_anchor_policy"] = "bounded-selection-v1"
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "revise-1"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert manifest["reader_review_schema_version"] == VERSION
    outputs = {
        name: journal.output(journal.invocation(options["run_id"], name))["output"]
        for name in ("draft", "plan", "reader-review", "fact-review")
    }
    pending = journal.request(journal.invocation(options["run_id"], "revise-1"))
    if repair:
        attempts = journal.attempts(options["run_id"], "reader-review")
        assert [row["operation_kind"] for row in attempts] == ["role", "anchor_repair"]
        assert attempts[0]["status"] == "validation_failed"
        assert outputs["reader-review"]["revision_scope"]["anchors"][0]["utterance_index"] == 0
    assert outputs["reader-review"]["revision_scope"]["reason"] == (
        "Синтетическое решение: заново выбрать порядок открытия и развязки всего рассказа."
    )
    visible = payload(pending)
    assert visible["revision_guidance"][0]["revision_scope"] == outputs["reader-review"]["revision_scope"]
    assert visible["revision_guidance"][0]["positive_observations"] == outputs["reader-review"]["strengths"]
    assert visible["editorial_frame"]["authority"] == "revisable_authorial_decisions"
    assert visible["assignment"]["question"] != visible["editorial_frame"]["main_question"]
    files = {}
    for name, output in outputs.items():
        files[name] = tmp_path / (name + ".json")
        files[name].write_text(json.dumps(output, ensure_ascii=False))
    preview = tmp_path / "rewrite-preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(shared_ref),
            stage="revise-1",
            profile=options["writer_profile"],
            draft=files["draft"],
            approved_plan=files["plan"],
            reader_feedback=files["reader-review"],
            fact_feedback=files["fact-review"],
            out=preview,
        )
        == 0
    )
    assert json.loads(preview.read_text())["request"] == pending

    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "revise-1") == pending
    )
    assert "whole_episode" in (tmp_path / "export/review-result.md").read_text()
    assert journal.manifest(options["run_id"]) == manifest
