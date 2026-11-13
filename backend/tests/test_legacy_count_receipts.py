"""Resume actual pre-receipt archive shapes through real PostgreSQL and SDK mocks."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import httpx2
import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.compiler import count_request
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.count_receipts import native_count
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_context import ResearchContext
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_research_runtime import answer, call, final, invoke, research_setup


def archive(repo, project, provider, *, pin_profile=True):
    local = pytest.MonkeyPatch()
    memory, _, limits, corpus = research_setup(local)
    local.undo()
    body = memory.request(memory.root)
    journal = EvaluationJournal(repo, project)
    profile_ref = create_profile(
        repo, project, model=body["model"], provider=provider, context_limit=100000, output_limit=24576
    ).ref
    profile = load_profile(repo, project, profile_ref, "draft")
    author = journal.record(dict(contract="legacy-count-test/1"), kind="user_input")
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="legacy-count",
        author_ref=author,
        writer_profile_ref=profile_ref,
        reviewer_profile_ref=profile_ref,
    )
    journal.create_run(command, dict(memory.snapshot, stages=["draft"], command=command_payload(command)))
    root = journal.prepare(
        command.run_id,
        "draft",
        body,
        dict(profile_ref=profile_ref.model_dump(mode="json")) if pin_profile else {},
    )
    return journal, root, profile, limits, corpus


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_ordinary_resume_reuses_legacy_count_without_repeating_paid_turn(repo, scope, monkeypatch, provider):
    journal, root, profile, limits, corpus = archive(repo, scope[0], provider)
    transport = httpx2 if provider == "openai" else httpx
    counts, submits = [], []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith(("/input_tokens", "/count_tokens")):
            counts.append(body)
            return transport.Response(200, json=dict(input_tokens=100))
        submits.append(body)
        turn = len(submits) - 1
        if provider == "openai":
            return answer(body, turn, [call(1)] if turn == 0 else [final()])
        return transport.Response(
            200,
            json=dict(
                type="message",
                role="assistant",
                model=body["model"],
                id=f"msg_{turn}",
                stop_reason="tool_use" if turn == 0 else "end_turn",
                content=[
                    dict(
                        type="tool_use",
                        id="call_1",
                        name="read_source",
                        input=dict(source_id="S1", start=0, end=4000),
                    )
                ]
                if turn == 0
                else [dict(type="text", text='{"answer":"done"}')],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    class Crash(Exception):
        pass

    # Exactly the old writer: no intent, native-request field, identity or configuration.
    def old_measured(context, body, index, label):
        saved = context.history.counted(digest(body))
        if saved:
            return saved.data["input_tokens"]
        reply = context.gateway.provider_factory(profile).count(count_request(body))
        context.event(
            "source_context_count",
            dict(
                request_hash=digest(body),
                input_tokens=100,
                turn_index=index,
                label=label,
            ),
            reply=reply,
        )
        if index == 1:
            raise Crash()
        return 100

    with transport.Client(transport=transport.MockTransport(handler)) as client:
        factory = (
            (lambda _: OpenAIProvider("test-secret-absent", http_client=client))
            if provider == "openai"
            else (lambda _: AnthropicProvider("test-secret-absent", http_client=client))
        )
        gateway = ModelGateway(journal, factory, source_corpus=corpus)
        with monkeypatch.context() as historical:
            historical.setattr(ResearchContext, "measured", old_measured)
            historical.setattr(ResearchContext, "receipt", lambda self, body: None)
            with pytest.raises(Crash):
                invoke(gateway, root, profile, limits)
        before = {
            event["record_id"]: journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
            )
            for invocation in [root, *model_turns.rows(journal, root)]
            for event in journal.events(invocation)
        }
        assert len(counts) == 3 and len(submits) == len(corpus.calls) == 1
        assert counts[0] == counts[1]  # Historical context and gateway counted the same native request.
        assert counts[2] != counts[0]
        resumed = journal.invocation(root["run_id"], "draft")
        assert invoke(gateway, resumed, profile, limits) == '{"answer":"done"}'
        assert len(counts) == 3 and len(submits) == 2 and len(corpus.calls) == 1
        # Compare the unfinished count with the request actually submitted after resume.
        rows = model_turns.rows(journal, root)
        assert counts[2] == native_count(journal.request(rows[1]), provider)
        assert (
            invoke(gateway, journal.invocation(root["run_id"], "draft"), profile, limits)
            == '{"answer":"done"}'
        )
        assert len(counts) == 3 and len(submits) == 2
    assert all(
        journal.read(RuntimeRef(record_kind="tool_execution", record_id=key)) == value
        for key, value in before.items()
    )


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "wire",
        "wire_type",
        "missing_wire",
        "redacted",
        "response",
        "status",
        "hash",
        "profile",
        "adapter",
        "provider",
        "timeout",
        "parameters",
        "parameter_type",
        "context_limit",
        "output_limit",
        "input",
        "schema",
        "tools",
    ],
)
def test_legacy_requires_native_response_and_archived_configuration_proof(repo, scope, change):
    journal, root, profile, _, _ = archive(repo, scope[0], "openai", pin_profile=change != "profile")
    body = journal.request(root)
    wire = native_count(body, profile.provider)
    data = dict(request_hash=digest(body), input_tokens=100, turn_index=0, label="normal")
    reply = dict(body=b'{"input_tokens":100}', status_code=200, request_body=json.dumps(wire).encode())
    current = SimpleNamespace(**profile.model_dump(mode="json"))
    if change == "wire":
        reply["request_body"] = b"{}"
    elif change == "wire_type":
        reply["request_body"] = json.dumps(dict(wire, parallel_tool_calls=0)).encode()
    elif change == "missing_wire":
        reply["request_body"] = None
    elif change == "redacted":
        reply["redacted"] = True
    elif change == "response":
        reply["body"] = b'{"input_tokens":101}'
    elif change == "status":
        reply["status_code"] = 400
    elif change == "hash":
        data["request_hash"] = "wrong"
    elif change == "adapter":
        current.adapter_version = "openai-responses-v99"
    elif change == "provider":
        current.provider = "anthropic"
    elif change == "timeout":
        current.timeout_policy["rpc_timeout_seconds"] += 1
    elif change == "parameters":
        current.generation_parameters["text"]["verbosity"] = "low"
    elif change == "parameter_type":
        current.generation_parameters["store"] = 0
    elif change == "context_limit":
        current.context_limit += 1
    elif change == "output_limit":
        current.output_limit += 1
    ref = journal.event(root, "source_context_count", data=data, reply=WireReply(**reply))
    if change == "input":
        body["input"][0]["content"][0]["text"] += " changed"
    elif change == "schema":
        body["text"]["format"]["schema"]["title"] = "Changed"
    elif change == "tools":
        body["tools"] = body["tools"][:-1]
    counts = []

    provider = SimpleNamespace(
        count=lambda request: counts.append(request) or WireReply(b'{"input_tokens":200}')
    )
    gateway = ModelGateway(journal, lambda _: provider)
    history = ResearchHistory(journal, root)
    context = ResearchContext(gateway, root, current, body, history, lambda: None)
    result = context.receipt(body)
    assert (result is not None) == (change == "none")
    if result:
        assert result.reference == ref and result.validate(journal, body, current) == 100

    assert not counts
    assert context.measured(body, 0, "normal") == (100 if change == "none" else 200)
    assert len(counts) == (0 if change == "none" else 1)
