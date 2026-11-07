"""Ordinary own-plan and imported-script paths expose exact previews and permit shortening."""

import json
from copy import deepcopy

import pytest

from podgen import writing_cli
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reading_stage import execute_reading_stage
from podgen.runtime.models.contracts import WireReply
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset


class EditingProvider(ReadingProvider):
    def __init__(self):
        super().__init__(("revise", "keep"), pause="revise-1")
        self.original = None

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        if stage in ("draft", "revise-1"):
            self.requests.append(deepcopy(body))
            speakers = payload(body)["assignment"]["speakers"]
            script = dict(
                utterances=[
                    dict(
                        speaker=s["label"],
                        text="Тестовый рассказ. " * 20 if stage == "draft" else "Краткая тестовая редакция.",
                    )
                    for _ in range(60 if stage == "draft" else 1)
                    for s in speakers
                ]
            )
            if stage == "draft":
                self.original = script
            result = script
            if stage == "revise-1" and "revision_operation" in payload(body):
                frame = payload(body).get("editorial_frame", {})
                result = dict(
                    script=script,
                    composition=dict(
                        main_question=frame.get("main_question", "Тестовый вопрос."),
                        listener_promise=frame.get("listener_promise", "Тестовое обещание."),
                        route_summary="Краткая новая тестовая композиция.",
                        decisions=["Сократить повторения."],
                    ),
                )
            return WireReply(
                json.dumps(
                    dict(
                        id="resp_" + stage.replace("-", "_"),
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=[
                            dict(
                                type="message",
                                role="assistant",
                                content=[
                                    dict(type="output_text", text=json.dumps(result, ensure_ascii=False))
                                ],
                            )
                        ],
                        usage=dict(input_tokens=100, output_tokens=100),
                    ),
                    ensure_ascii=False,
                ).encode()
            )
        reply = super().submit(body, client_request_id=client_request_id)
        if stage == "reader-review" and body["text"]["format"]["name"] != "anchor_choices":
            envelope = json.loads(reply.body)
            content = envelope["output"][0]["content"][0]
            review = json.loads(content["text"])
            quote = payload(body)["script"]["utterances"][0]["text"][:25]
            review["strengths"] = [
                dict(
                    anchors=[dict(utterance_index=0, quote=quote)],
                    reason="Полезна связность рассказа, а не буквальное сохранение этого абзаца.",
                )
            ]
            review["findings"][0]["requested_change"] = (
                "Сократить повторяющийся обмен, сохранив понятную причинную связь."
            )
            review["revision_scope"]["scale"] = "whole_episode"
            content["text"] = json.dumps(review, ensure_ascii=False)
            return WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        return reply


@pytest.mark.parametrize("mode", ["own", "imported"])
def test_runtime_previews_and_revisions_preserve_required_advice_without_freezing_paragraphs(
    own_dataset, tmp_path, monkeypatch, mode
):
    if mode == "own":
        prefix, _, options, _, ref = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
        options["review_mode"] = "independent-readings/1"
        script = None
    else:
        prefix, options, script = setup_review(own_dataset, tmp_path, monkeypatch)
        ref = tmp_path / "shared-ref.json"
    provider = EditingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    script = script or provider.original
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    pending = journal.invocation(options["run_id"], "revise-1")
    rewrite = journal.request(pending)
    guidance = payload(rewrite)["revision_guidance"]
    for origin, stage in (("reader", "reader-review"), ("fact", "fact-review")):
        feedback = journal.output(journal.invocation(options["run_id"], stage))["output"]
        projected = next(row for row in guidance if row["origin"] == origin)
        assert projected["findings"] == feedback["findings"]
        assert projected["positive_observations"] == feedback["strengths"]
    assert "retain_passages" not in guidance[0]
    script_path = tmp_path / "actual-script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False))
    preview = tmp_path / "reader-preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(ref),
            stage="reader-review",
            profile=options["reviewer_profile"],
            draft=script_path,
            out=preview,
        )
        == 0
    )
    first_reader = next(
        body for body in provider.requests if body["metadata"]["podgen_stage"] == "reader-review"
    )
    assert json.loads(preview.read_text())["request"] == first_reader
    if mode == "own":
        plan = journal.output(journal.invocation(options["run_id"], "plan"))["output"]
        plan_path = tmp_path / "accepted-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False))
        writer_preview = tmp_path / "writer-preview.json"
        assert (
            invoke(
                prefix,
                "preview",
                run_id=options["run_id"],
                case="snow-pump",
                shared_ref="@" + str(ref),
                stage="draft",
                profile=options["writer_profile"],
                approved_plan=plan_path,
                out=writer_preview,
            )
            == 0
        )
        assert json.loads(writer_preview.read_text())["request"] == next(
            body for body in provider.requests if body["metadata"]["podgen_stage"] == "draft"
        )
        context = journal.read(
            RuntimeRef(record_kind="tool_execution", record_id=pending["request_record_id"])
        )["context"]
        assert context["approved_plan_hash"] == digest(plan)
        command = WritingRunCommand.model_validate(manifest["command"])
        generation = generation_repo(own_dataset[0])
        materials = load_editorial_materials(
            generation, own_dataset[1], command.author_ref, case_id="snow-pump"
        )
        changed_plan = deepcopy(plan)
        changed_plan["plan"]["route_summary"] += " Другой маршрут."
        with pytest.raises(Conflict, match="independent_reading_binding_mismatch"):
            execute_reading_stage(
                "revise-1",
                journal=journal,
                gateway=None,
                generation=generation,
                command=command,
                manifest=manifest,
                materials=materials,
                totals=dict(input=0, output=0),
                guard=lambda: None,
                script=script,
                script_ref=context["script_ref"],
                reading_refs=context["reading_refs"],
                readings={
                    origin: journal.output(journal.invocation(options["run_id"], stage))["output"]
                    for origin, stage in (("reader", "reader-review"), ("fact", "fact-review"))
                },
                approved_plan=changed_plan,
            )
    provider.pause = None
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "revise-1") == rewrite
    )
    readers = [
        payload(body)
        for body in provider.requests
        if body["metadata"]["podgen_stage"].startswith("reader-review")
    ]
    assert (
        readers[1]["duration_context"]["current_text"]["word_count"]
        < readers[0]["duration_context"]["current_text"]["word_count"]
    )
    assert readers[1]["script"]["utterances"][0]["text"] != readers[0]["script"]["utterances"][0]["text"]
    assert manifest == journal.manifest(options["run_id"])
