"""Real SDK wire over durable research windows; no provider network calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx2
import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.source_tool_contract import create
from podgen.runtime.evaluation.source_tool_trace import successful_read_passages
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.evaluation.wire_budget import MODEL_WINDOW_BUDGET
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.test_source_tool_runtime import call, final, setup


def research_setup(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch)
    contract = create(
        SimpleNamespace(name="production/1"),
        SimpleNamespace(packet=SimpleNamespace(contract="research-editorial-input/1")),
    )
    body = journal.request(journal.root)
    body["tools"] = contract["definitions"]
    body["metadata"]["podgen_prompt_cache_layout"] = "research-role-prefix/2"
    journal.root["request_record_id"] = journal.store_request(body)
    journal.snapshot.update(source_tool_contract=contract, role_input_budget=MODEL_WINDOW_BUDGET)
    corpus.material_resolver = None
    return journal, profile, limits, corpus


def invoke(gateway, root, profile, limits, *, guard=lambda: None, totals=None):
    return gateway.invoke(
        root.copy(), profile, limits=limits, totals=totals or dict(input=0, output=0), guard=guard
    )


def answer(body, turn, output):
    return httpx2.Response(
        200,
        json=dict(
            id=f"resp_{turn}",
            model=body["model"],
            metadata=body["metadata"],
            status="completed",
            output=output,
            usage=dict(input_tokens=100, output_tokens=10),
        ),
    )


def test_reading_exceeds_old_turn_and_call_limits_and_recovers_final_publication(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 100})
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        assert body["tool_choice"] == "auto"
        return answer(body, turn, [call(turn + 1)] if turn < 26 else [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert len(corpus.calls) == 26 and len(sent) == 27
        journal.root["status"] = "tool_running"  # crash after final event, before aggregate/status
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
        assert journal.root["status"] == "response_completed"
        assert len(sent) == 27 and len(corpus.calls) == 26


def test_real_count_rotates_full_transcript_and_compact_state_then_resumes(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    sent = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            has_result = any(item.get("type") == "function_call_output" for item in body["input"])
            return httpx2.Response(200, json={"input_tokens": 20000 if has_result else 100})
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn == 0:
            return answer(
                body,
                turn,
                [
                    dict(type="reasoning", id="rs_exact", summary=[], encrypted_content="opaque-exact"),
                    call(1),
                ],
            )
        if turn == 1:
            assert [tool["name"] for tool in body["tools"]] == ["save_working_state"]
            return answer(
                body,
                turn,
                [
                    dict(
                        type="function_call",
                        call_id="checkpoint_1",
                        name="save_working_state",
                        status="completed",
                        arguments=json.dumps(
                            dict(
                                state="FULL NOTES " * 1000,
                                resume_state="Continue checking the exact E1 passage.",
                                receipt_ids=["E1"],
                            )
                        ),
                    )
                ],
            )
        assert "FULL NOTES" not in json.dumps(body["input"])
        assert "Continue checking the exact E1 passage." in json.dumps(body["input"])
        return answer(body, turn, [final()])

    class Pause(Exception):
        pass

    def guard():
        if any(event["event_kind"] == "source_context_segment" for event in journal.receipts):
            raise Pause()

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(Pause):
            invoke(gateway, journal.root, profile, limits, guard=guard)
        assert len(sent) == 2 and len(corpus.calls) == 1
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(sent) == 3 and len(corpus.calls) == 1
    segment_event = next(
        event for event in journal.receipts if event["event_kind"] == "source_context_segment"
    )
    archived = journal.records[segment_event["record_id"]]["data"]["conversation_blob"]
    transcript = json.loads(journal.blobs[archived["sha256"]])
    assert transcript[1]["encrypted_content"] == "opaque-exact"
    assert "Full exact source passage" in json.dumps(transcript)
    state = next(
        journal.records[event["record_id"]]["data"]["arguments"]["state"]
        for event in journal.receipts
        if event["event_kind"] == "source_tool_result"
        and journal.records[event["record_id"]]["data"]["name"] == "save_working_state"
    )
    assert state == "FULL NOTES " * 1000


def test_credit_retry_after_two_reads_preserves_exact_failed_request_and_receipts(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    sent = []
    refused = False

    def handler(request):
        nonlocal refused
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 100})
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn == 2 and not refused:
            refused = True
            return httpx2.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        return answer(body, len(sent), [call(turn + 1)] if turn < 3 else [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        parent = journal.root.copy()
        paid = attempt_usage(journal, parent, profile.output_limit)
        assert paid == dict(input=200, output=20)
        child = authorize_credit_retry(journal, parent["run_id"], parent["invocation_id"], guard=lambda: None)
        assert invoke(gateway, child, profile, limits, totals=paid) == '{"answer":"done"}'
    assert [int(body["metadata"]["podgen_source_turn"]) for body in sent] == [0, 1, 2, 2, 3]
    assert {k: v for k, v in sent[2].items() if k != "metadata"} == {
        k: v for k, v in sent[3].items() if k != "metadata"
    }
    assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2", "E3"]
    assert [row["read_id"] for row in successful_read_passages(journal, child)] == ["E1", "E2", "E3"]


def test_real_model_window_allows_more_than_old_token_and_byte_limits(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    body = journal.request(journal.root)
    body["instructions"] = "Exact full instruction. " * 16000
    journal.root["request_record_id"] = journal.store_request(body)
    journal.records[journal.root["request_record_id"]]["context"] = {
        "role_input_contract": "editorial-role-packets/1"
    }
    journal.snapshot["input_contract"] = "editorial-role-packets/1"
    sent = []

    def handler(request):
        native = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 70000})
        sent.append(native)
        return answer(native, 0, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert sent[0]["instructions"] == body["instructions"] and len(body["instructions"].encode()) > 256 * 1024


def test_pending_material_request_resumes_the_same_paid_response(monkeypatch):
    from podgen.sources.material_requests import MaterialResolution

    journal, profile, limits, corpus = research_setup(monkeypatch)
    corpus._materials = SimpleNamespace(runtime_admissions=[])
    corpus.refresh = lambda: None
    operations, sent = [], []

    class Resolver:
        pending = True

        def restore(self, **kwargs):
            return ()

        def resolve(self, name, args, **kwargs):
            operations.append(kwargs["operation_id"])
            return MaterialResolution(
                dict(status="pending", handoff_ref={"saved": True})
                if self.pending
                else dict(status="resolved", source_id="S1")
            )

    resolver = Resolver()
    corpus.material_resolver = resolver

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 100})
        sent.append(body)
        output = (
            [
                dict(
                    type="function_call",
                    call_id="need_document",
                    name="request_source",
                    status="completed",
                    arguments=json.dumps(dict(source_id="S1", url=None, reason="Exact unresolved date.")),
                )
            ]
            if len(sent) == 1
            else [final()]
        )
        return answer(body, len(sent), output)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="source_material_handoff_pending"):
            invoke(gateway, journal.root, profile, limits)
        assert len(sent) == 1 and not any(
            event["event_kind"] == "source_tools_final_response" for event in journal.receipts
        )
        resolver.pending = False
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(sent) == 2 and len(operations) == 2 and len(set(operations)) == 1


@pytest.mark.parametrize("mode", ["credit", "count_after_rotation"])
def test_real_database_operational_retry_keeps_parent_receipts_and_exact_request(repo, scope, mode):
    from uuid import uuid4

    from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.profiles import create_profile

    local = pytest.MonkeyPatch()
    memory, profile, limits, corpus = research_setup(local)
    local.undo()  # use the real SQL model-turn projection, not the unit-test adapter
    journal = EvaluationJournal(repo, scope[0])
    ref = create_profile(repo, scope[0], model="gpt-6.1-sol", context_limit=100000).ref
    author = journal.record(dict(contract="runtime-test-material/1"), kind="user_input")
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id="runtime",
        author_ref=author,
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
    )
    manifest = dict(memory.snapshot, command=command_payload(command), stages=["draft"])
    journal.create_run(command, manifest)
    body = memory.request(memory.root)
    root = journal.prepare(command.run_id, "draft", body, {"profile_ref": ref.model_dump(mode="json")})
    sent, counts = [], {}
    failed = False
    failed_count = None

    def handler(request):
        nonlocal failed, failed_count
        native = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            key = json.dumps(native, sort_keys=True)
            counts[key] = counts.get(key, 0) + 1
            compact = "runtime_continuation" in json.dumps(native["input"])
            if mode == "count_after_rotation" and compact and counts[key] == 1 and not failed:
                failed = True
                failed_count = deepcopy(native)
                return httpx2.Response(503, json={"error": {"message": "temporary count unavailable"}})
            large = mode == "count_after_rotation" and any(
                item.get("type") == "function_call_output" for item in native["input"]
            )
            return httpx2.Response(200, json={"input_tokens": 20000 if large else 100})
        sent.append(native)
        turn = int(native["metadata"]["podgen_source_turn"])
        if mode == "credit" and turn == 2 and not failed:
            failed = True
            return httpx2.Response(429, json={"error": {"code": "credit_balance_exhausted"}})
        if mode == "count_after_rotation" and turn == 1:
            output = [
                dict(
                    type="function_call",
                    call_id="checkpoint",
                    name="save_working_state",
                    status="completed",
                    arguments=json.dumps(
                        dict(
                            state="Full durable notes.",
                            resume_state="Check E1 before finishing.",
                            receipt_ids=["E1"],
                        )
                    ),
                )
            ]
        elif turn < (2 if mode == "credit" else 1):
            output = [
                dict(type="reasoning", id=f"rs_{turn}", summary=[], encrypted_content=f"opaque_{turn}"),
                call(turn + 1),
            ]
        else:
            output = [final()]
        return answer(native, len(sent), output)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, root, profile, limits)
        parent = journal.invocation(command.run_id, "draft")
        paid = attempt_usage(journal, parent, profile.output_limit)
        if mode == "credit":
            child = authorize_credit_retry(
                journal, command.run_id, parent["invocation_id"], guard=lambda: None
            )
        else:
            # Context counting failed before the next physical turn was prepared.
            # Resume the same durable window; completed paid turns are replayed.
            assert parent["status"] == "tool_running"
            child = parent
        from podgen.runtime.models.responses_wire import responses_request

        failed_request = responses_request(journal.request(child))
        assert (
            invoke(gateway, child, profile, limits, totals=paid if mode == "credit" else None)
            == '{"answer":"done"}'
        )
        if mode == "credit":
            assert {k: v for k, v in sent[-1].items() if k != "metadata"} == {
                k: v for k, v in failed_request.items() if k != "metadata"
            }
        else:
            assert failed_count == {k: v for k, v in sent[-1].items() if k in failed_count}
            assert len(sent) == 3
            for event in journal.events(root):
                if event["event_kind"] not in {
                    "source_context_count_intent",
                    "source_context_count",
                    "source_context_count_error",
                }:
                    continue
                record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=event["record_id"]))
                assert record["invocation_id"] == str(root["invocation_id"])
                assert record["request_ref"]["record_id"] == str(root["request_record_id"])
        completed = journal.invocation(command.run_id, "draft")
        before = len(sent)
        assert invoke(gateway, completed, profile, limits, totals=paid) == '{"answer":"done"}'
        assert len(sent) == before
    expected = ["E1", "E2"] if mode == "credit" else ["E1"]
    assert [options["read_id"] for _, _, options in corpus.calls] == expected
    assert [row["read_id"] for row in successful_read_passages(journal, child)] == expected
    accounting = journal.execution.current_head(command.run_id)["accounting"]
    assert accounting["count_calls"] == sum(counts.values())
    assert accounting.get("uncertain_calls", 0) == 0
    if mode == "count_after_rotation":
        errors = [
            event for event in journal.events(root) if event["event_kind"] == "source_context_count_error"
        ]
        assert len(errors) == 1
        error_ref = RuntimeRef(record_kind="tool_execution", record_id=errors[0]["record_id"])
        saved_error = journal.read(error_ref)
        assert saved_error["provider_http_status"] == 503
        assert saved_error["data"]["retryable"] is True


def test_optional_planning_questions_are_retained_without_creating_a_mandatory_handoff():
    from podgen.runtime.models.research_continuation import structured_requests

    request = dict(
        question="Optional wider context", why_needed="Interesting but not necessary", blocking=False
    )
    response = dict(
        output=[
            dict(
                type="message",
                content=[dict(type="output_text", text=json.dumps(dict(research_requests=[request])))],
            )
        ]
    )
    history = SimpleNamespace(of_kind=lambda _: [])
    assert structured_requests(response, 0, history) == []
    request["blocking"] = True
    response["output"][0]["content"][0]["text"] = json.dumps(dict(research_requests=[request]))
    assert structured_requests(response, 0, history) == [request]


def test_structured_research_request_pauses_before_final_review_and_resumes_same_response(monkeypatch):
    from podgen.sources.material_requests import MaterialResolution

    journal, profile, limits, corpus = research_setup(monkeypatch)
    corpus._materials = SimpleNamespace(runtime_admissions=[])
    corpus.refresh = lambda: None
    sent, operations = [], []

    class Resolver:
        pending = True

        def restore(self, **kwargs):
            return ()

        def resolve(self, name, args, **kwargs):
            assert name == "request_research" and args["question"] == "Exact missing question?"
            operations.append(kwargs["operation_id"])
            return MaterialResolution(
                dict(status="pending", handoff_ref={"saved": True})
                if self.pending
                else dict(status="resolved", report_id="X1")
            )

    resolver = Resolver()
    corpus.material_resolver = resolver

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 100})
        sent.append(body)
        if len(sent) == 1:
            output = [
                dict(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        dict(
                            type="output_text",
                            text=json.dumps(
                                dict(
                                    answer="unfinished",
                                    research_requests=[
                                        dict(
                                            question="Exact missing question?",
                                            why_needed="Verify the necessary account.",
                                        )
                                    ],
                                )
                            ),
                        )
                    ],
                )
            ]
        else:
            assert "material_request_resolutions" in json.dumps(body["input"])
            output = [final()]
        return answer(body, len(sent), output)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="source_material_handoff_pending"):
            invoke(gateway, journal.root, profile, limits)
        assert len(sent) == 1
        resolver.pending = False
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(sent) == 2 and len(operations) == 2 and len(set(operations)) == 1
