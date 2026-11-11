"""A native repaired /2 review survives completion, frozen preview, export and resume."""

import json
from copy import deepcopy

from podgen import writing_cli
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.models.contracts import WireReply
from tests.test_editorial_independence import invoke, payload
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_required_recheck_runtime import RequiredTaskProvider


class RepairedRecheckProvider(RequiredTaskProvider):
    def __init__(self):
        super().__init__(False)

    def submit(self, body, *, client_request_id):
        if body["text"]["format"]["name"] == "anchor_choices":
            return ReadingProvider.submit(self, body, client_request_id=client_request_id)
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] != "reader-review-1":
            return reply
        envelope = json.loads(reply.body)
        content = envelope["output"][0]["content"][0]
        review = json.loads(content["text"])
        review["strengths"] = [
            dict(
                anchors=[dict(utterance_index=999, quote=payload(body)["script"]["utterances"][0]["text"])],
                reason="The revised opening now makes the connection.",
            )
        ]
        content["text"] = json.dumps(review, ensure_ascii=False)
        return WireReply(json.dumps(envelope, ensure_ascii=False).encode())


def test_repaired_production_recheck_completes_and_replays_without_new_calls(
    own_dataset, tmp_path, monkeypatch
):
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    repo, project, *_ = own_dataset
    for role, model, provider_name in (
        ("writer", "claude-opus-5-5", "anthropic"),
        ("reviewer", "gpt-6.1-terra", "openai"),
    ):
        options[role + "_profile"] = create_profile(
            repo,
            project,
            model=model,
            provider=provider_name,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref.model_dump_json()
    options.update(behavior_profile="production/1", review_anchor_policy="bounded-selection-v1")
    provider = RepairedRecheckProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *args, **kwargs: provider
    )
    assert invoke(prefix, "run", **options) == 0
    journal = EvaluationJournal(repo, project)
    run = options["run_id"]
    attempts = journal.attempts(run, "reader-review-1")
    assert [row["operation_kind"] for row in attempts] == ["role", "anchor_repair"]
    assert [row["status"] for row in attempts] == ["validation_failed", "completed"]
    original, repaired = (journal.request(row) for row in attempts)
    assert "revision_check" in payload(original) and "revision_check" not in payload(repaired)
    result = journal.output(attempts[-1])["output"]
    assert result["recommendation"] == "keep" and result["strengths"][0]["anchors"][0]["utterance_index"] == 0
    assert read_outcome(journal, run)["status"] == "keep"
    script = journal.output(journal.invocation(run, "revise-1"))["output"]
    source = tmp_path / "current.json"
    source.write_text(json.dumps(script, ensure_ascii=False))
    assert (
        invoke(
            prefix,
            "preview",
            run_id=run,
            case=options["case"],
            shared_ref=options["author_ref"],
            profile=options["reviewer_profile"],
            stage="reader-review-1",
            draft=source,
            out=tmp_path / "preview.json",
        )
        == 0
    )
    assert json.loads((tmp_path / "preview.json").read_text())["request"] == original
    sent = deepcopy(provider.requests)
    export_run(repo, project, run, tmp_path / "second-export")
    assert invoke(prefix, "resume", run_id=run, export_dir=tmp_path / "resumed-export") == 0
    assert provider.requests == sent
    assert journal.request(attempts[0]) == original and journal.request(attempts[1]) == repaired
