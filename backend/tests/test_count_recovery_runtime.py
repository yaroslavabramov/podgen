"""Count recovery through ordinary resume and durable tool turns; local providers only."""

import json

import httpx
import pytest
from podgen.runtime.evaluation.contracts import RunFailure, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.evaluation.technical_recovery import resume_current
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider

from tests.test_source_tool_recovery import invoke
from tests.test_source_tool_runtime import call, final, setup
from tests.test_writing_evaluation import FakeProvider
from tests.test_writing_evaluation import setup_writer as setup_writer


def test_ordinary_resume_recovers_saved_count_failure_without_unknown_generation_usage(
    repo, setup_writer, tmp_path
):
    command, loader, _ = setup_writer
    provider = FakeProvider()
    count = provider.count
    provider.count = lambda _: (_ for _ in ()).throw(ProviderFault("provider_transport_error"))
    runner = WritingEvaluation(
        repo, lambda _: provider, materials_loader=loader, legacy_offline_transport=provider
    )
    with pytest.raises(RunFailure, match="provider_transport_error"):
        runner.run(command)
    journal = EvaluationJournal(repo, command.project_id)
    parent = journal.invocation(command.run_id, "draft")
    assert attempt_usage(journal, parent, 65536) == dict(input=0, output=0)
    provider.count = count
    result = runner.run(command)
    assert result["status"] == "completed_unaccepted"
    assert result["token_usage_or_reservation"] == dict(input=300, output=369)
    sent = len(provider.requests)
    assert runner.run(command) == result and len(provider.requests) == sent
    assert [r["status"] for r in journal.attempts(command.run_id, "draft")] == ["failed", "completed"]
    from podgen.runtime.evaluation.export import export_run

    exported = export_run(repo, command.project_id, command.run_id, tmp_path / "export")
    assert exported["unknown_usage_invocations"] == []


def test_durable_fourth_count_failure_preserves_three_paid_turns_and_receipts(repo, setup_writer):
    command, _, _ = setup_writer
    with pytest.MonkeyPatch.context() as patch:
        memory, profile, limits, corpus = setup(patch, turns=5)
        snapshot, body = memory.snapshot, memory.request(memory.root)
    journal = EvaluationJournal(repo, command.project_id)
    snapshot.update(stages=["draft"], command=command_payload(command), contract="writing-evaluation-run-v1")
    counts, sent = [], []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            counts.append(body)
            if len(counts) == 4:
                raise httpx.ReadTimeout("Count interrupted", request=request)
            return httpx.Response(200, json=dict(input_tokens=100))
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{turn}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(turn + 1)] if turn < 3 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with journal.lock(command.run_id), httpx.Client(transport=httpx.MockTransport(handler)) as client:
        journal.create_run(command, snapshot)
        parent = journal.prepare(
            command.run_id,
            "draft",
            body,
            dict(profile_ref=command.writer_profile_ref.model_dump(mode="json")),
        )
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="provider_transport_error"):
            invoke(gateway, parent, profile, limits)
        parent = journal.invocation(command.run_id, "draft")
        paid = attempt_usage(journal, parent, profile.output_limit)
        assert paid == dict(input=300, output=30)
        child = resume_current(
            journal, command, gateway=gateway, load_profile=lambda ref, stage: profile, guard=lambda: None
        )
        assert invoke(gateway, child, profile, limits, paid) == '{"answer":"done"}'
        assert attempt_usage(journal, parent, profile.output_limit) == paid
        assert attempt_usage(journal, child, profile.output_limit) == dict(input=100, output=10)
    assert [row["metadata"]["podgen_source_turn"] for row in sent] == ["0", "1", "2", "3"]
    assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2", "E3"]
    assert counts[3] == counts[4]
