"""Same-process private/3 repair uses the durable response, with no repeat of the rejected review."""

import json
from uuid import UUID, uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.episodes.production.prepare import prepare
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.review_repair import prepare_anchor_repair
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from tests.episode_production_support import setup_episode
from tests.research_material_provider import ResearchProvider
from tests.revision_providers import reply


class BrokenAnchorProvider(ResearchProvider):
    def __init__(self):
        super().__init__()
        self.rejected = None

    def submit(self, body, *, client_request_id):
        if body["text"]["format"]["name"] == "anchor_choices":
            self.requests.append(body)
            data = request_payload(body)
            return reply(
                body,
                dict(
                    choices=[
                        dict(anchor_id=row["anchor_id"], selector_id=row["selector_ids"][0])
                        for row in data["invalid_anchors"]
                    ]
                ),
            )
        response = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == "reader-review":
            envelope = json.loads(response.body)
            content = envelope["output"][0]["content"][0]
            feedback = json.loads(content["text"])
            feedback["audit"]["judgments"][0]["anchors"][0]["quote"] += " Typo."
            self.rejected = feedback
            content["text"] = json.dumps(feedback)
            return WireReply(json.dumps(envelope).encode())
        return response


def test_same_process_native_repair_reuses_binding_and_keeps_original_response(
    repo, scope, producer, monkeypatch
):
    config, runner, _, _, _, _ = setup_episode(repo, scope, producer, monkeypatch)
    models = BrokenAnchorProvider()
    runner.writer.provider_factory = lambda _: models
    native_run = runner.writer.run
    monkeypatch.setattr(runner.writer, "run", lambda command: native_run(command, repair_review_anchors=True))
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled"
    journal = EvaluationJournal(repo, scope[0])
    run_id = UUID(result["progress"]["writing_run_id"])
    original, repair = journal.attempts(run_id, "reader-review")
    assert original["status"] == "validation_failed" and original["provider_response_id"]
    assert repair["status"] == "completed" and repair["parent_invocation_id"] == original["invocation_id"]
    assert repair["operation_kind"] == "anchor_repair"
    envelope = json.loads(journal.last_response(original))
    assert envelope["id"] == original["provider_response_id"]
    assert envelope["metadata"] == journal.request(original)["metadata"]
    received = json.loads(envelope["output"][0]["content"][0]["text"])
    assert received == models.rejected
    expected = json.loads(json.dumps(received))
    expected["audit"]["judgments"][0]["anchors"][0]["quote"] = expected["audit"]["judgments"][0]["anchors"][
        0
    ]["quote"].removesuffix(" Typo.")
    assert journal.output(repair)["output"] == expected
    reader_calls = [body for body in models.requests if body["metadata"]["podgen_stage"] == "reader-review"]
    assert (
        len(reader_calls) == 2
        and sum(body["text"]["format"]["name"] == "anchor_choices" for body in reader_calls) == 1
    )
    paid = len(models.requests)
    assert runner.run() == result and len(models.requests) == paid

    def forbidden(profile):
        pytest.fail("cold replay must not construct a provider or repeat the rejected review")

    command = WritingRunCommand.model_validate(journal.manifest(run_id)["command"])
    assert WritingEvaluation(repo, forbidden).run(command)["review_status"] == "keep"

    # A stale descriptor still resolves to the same existing repair. A crossed
    # request identity never gains eligibility by reloading a different row.
    script_ref = RuntimeRef.model_validate(
        journal.read(RuntimeRef(record_kind="tool_execution", record_id=original["request_record_id"]))[
            "context"
        ]["script_ref"]
    )
    script = journal.read(script_ref)["output"]
    stale = dict(original, provider_response_id=None, input_tokens=None, output_tokens=None)
    assert (
        prepare_anchor_repair(
            journal, stale, draft=script, draft_ref=script_ref.model_dump(mode="json"), guard=lambda: None
        )
        == repair
    )
    with pytest.raises(RunFailure, match="not_eligible"):
        prepare_anchor_repair(
            journal,
            dict(stale, request_record_id=uuid4()),
            draft=script,
            draft_ref=script_ref.model_dump(mode="json"),
            guard=lambda: None,
        )
