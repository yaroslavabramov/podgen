"""The normal product cannot finalize a contradictory required-task review."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from podgen import writing_cli
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.models.contracts import WireReply
from tests.test_editorial_independence import invoke, payload
from tests.test_independent_readings import setup_review
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_revision_verification_runtime import CheckingProvider


def contradict(review, script):
    result = deepcopy(review)
    result.update(
        recommendation="keep",
        revision_scope=dict(scale="none", reason="Erroneous approval.", anchors=[], dependent_passages=[]),
        findings=[
            dict(
                anchors=[dict(utterance_index=0, quote=script["utterances"][0]["text"])],
                dimension="transition",
                priority="optional",
                observation="The required connection remains missing.",
                requested_change="Complete the assigned connection.",
                preserve=[],
            )
        ],
    )
    result["assignment_results"][0].update(outcome="still_open", followup_finding=0)
    return result


class RequiredTaskProvider(CheckingProvider):
    def __init__(self, invalid):
        super().__init__(("revise", "keep"))
        self.invalid = invalid

    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] != "reader-review-1":
            return reply
        envelope = json.loads(reply.body)
        content = envelope["output"][0]["content"][0]
        review = json.loads(content["text"])
        review["revision_scope"] = dict(
            scale="none", reason="No required repairs.", anchors=[], dependent_passages=[]
        )
        if self.invalid:
            review = contradict(review, payload(body)["script"])
        content["text"] = json.dumps(review, ensure_ascii=False)
        return WireReply(json.dumps(envelope, ensure_ascii=False).encode())


@pytest.mark.parametrize(
    "invalid_from_provider", [True, False], ids=["fresh-rejection", "saved-outcome-rejection"]
)
def test_normal_cycle_rejects_open_required_task_before_false_finalization(
    own_dataset, tmp_path, monkeypatch, invalid_from_provider
):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    repo, project, *_ = own_dataset
    for role, model, provider_name in (
        ("writer", "claude-opus-5-5", "anthropic"),
        ("reviewer", "gpt-6.1-terra", "openai"),
    ):
        ref = create_profile(
            repo,
            project,
            model=model,
            provider=provider_name,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
        options[role + "_profile"] = ref.model_dump_json()
    options["behavior_profile"] = "production/1"
    options["review_anchor_policy"] = "bounded-selection-v1"
    provider = RequiredTaskProvider(invalid_from_provider)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *args, **kwargs: provider
    )
    assert invoke(prefix, "run", **options) == (2 if invalid_from_provider else 0)
    journal = EvaluationJournal(repo, project)
    row = journal.invocation(options["run_id"], "reader-review-1")
    if invalid_from_provider:
        assert row["status"] == "validation_failed" and row["output_record_id"] is None
        assert journal.invocation(options["run_id"], "fact-review-1") is None
        attempts = journal.attempts(options["run_id"], "reader-review-1")
        assert len(attempts) == 1 and attempts[0]["operation_kind"] == "role"
        assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
            "reader-review",
            "fact-review",
            "revise-1",
            "reader-review-1",
        ]
        assert read_outcome(journal, options["run_id"]) is None
        assert not (tmp_path / "export/final.txt").exists()
        return
    assert read_outcome(journal, options["run_id"])["status"] == "keep"
    assert journal.invocation(options["run_id"], "revise-2") is None
    requests = deepcopy(provider.requests)
    original_output = EvaluationJournal.output
    current = journal.output(journal.invocation(options["run_id"], "revise-1"))["output"]

    def invalid_saved_review(self, invocation):
        record = original_output(self, invocation)
        if invocation["stage"] == "reader-review-1":
            record = deepcopy(record)
            record["output"] = contradict(record["output"], current)
        return record

    # Simulate a finalized /2 record written before the validator fix. The
    # original immutable fixture is not modified in storage.
    with monkeypatch.context() as corrupt:
        corrupt.setattr(EvaluationJournal, "output", invalid_saved_review)
        with pytest.raises(ValidationError, match="keep_has_open_task"):
            read_outcome(journal, options["run_id"])
        with pytest.raises(ValidationError, match="keep_has_open_task"):
            export_run(repo, project, options["run_id"], tmp_path / "invalid-export")
        assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "invalid-resume") == 2
    assert provider.requests == requests
