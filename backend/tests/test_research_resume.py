"""Same native request after continuous execution or an addressed paid-turn restart."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.sources.material_requests import MaterialResolution
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import call, final


def scenario(monkeypatch, *, structured, pause, checkpoint=False, prefix=4):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    journal.root.update(invocation_id=UUID(int=101), run_id=UUID(int=102))
    journal.roots = {journal.root["invocation_id"]: journal.root}
    profile.context_limit = 200000
    corpus._materials = SimpleNamespace(runtime_admissions=[])
    corpus.refresh = lambda: None
    sent, resolutions = [], []
    pending = pause

    class Resolver:
        def restore(self, **options):
            return ()

        def resolve(self, name, arguments, **options):
            resolutions.append((arguments["question"], options["operation_id"]))
            if pending and arguments["question"] == "waiting question":
                return MaterialResolution(dict(status="pending"))
            return MaterialResolution(dict(status="resolved", report_id="X1"))

    corpus.material_resolver = Resolver()

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            large = '"status":"resolved"' in json.dumps(body).replace('\\"', '"').replace(" ", "")
            count = 180000 if checkpoint and large and len(body["tools"]) > 1 else 100
            return httpx2.Response(200, json=dict(input_tokens=count))
        sent.append(deepcopy(body))
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn < prefix:
            output = [call(turn + 1)]
        elif turn == prefix and structured:
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
                                    research_requests=[
                                        dict(question="ready question", why_needed="Comparison"),
                                        dict(question="waiting question", why_needed="Verification"),
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ]
        elif turn == prefix:
            output = [
                call(1001),
                dict(
                    type="function_call",
                    status="completed",
                    name="request_research",
                    call_id="research",
                    arguments=json.dumps(dict(question="waiting question", reason="Verify")),
                ),
                call(1003),
            ]
        elif [tool.get("name") for tool in body["tools"]] == ["save_working_state"]:
            output = [
                dict(
                    type="function_call",
                    status="completed",
                    name="save_working_state",
                    call_id="checkpoint",
                    arguments=json.dumps(dict(state="Complete notes", resume_state="Finish", receipt_ids=[])),
                )
            ]
        else:
            output = [final()]
        return answer(body, turn, output)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:

        def factory(_):
            return OpenAIProvider("local", http_client=client)

        gateway = ModelGateway(journal, factory, source_corpus=corpus)
        if pause:
            with pytest.raises(RunFailure, match="source_material_handoff_pending"):
                invoke(gateway, journal.root, profile, limits)
            prefix_hashes = {
                journal.records[row["request_record_id"]]["request_blob"]["sha256"]
                for row in journal.turns
                if row["turn_index"] < prefix
            }
            original = journal.read_blob

            def addressed(reference, blob):
                assert BlobRef.model_validate(blob).sha256 not in prefix_hashes, (
                    "Old physical request reopened"
                )
                return original(reference, blob)

            journal.read_blob = addressed
            # Another cold attempt while the child is still pending must not resend
            # any paid turn or invoke completed calls from the same response.
            with pytest.raises(RunFailure, match="source_material_handoff_pending"):
                invoke(ModelGateway(journal, factory, source_corpus=corpus), journal.root, profile, limits)
            pending = False
        assert invoke(ModelGateway(journal, factory, source_corpus=corpus), journal.root, profile, limits)
        previous = len(sent)
        assert invoke(ModelGateway(journal, factory, source_corpus=corpus), journal.root, profile, limits)
        assert len(sent) == previous
    return sent, corpus.calls, resolutions


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("checkpoint", [False, True])
def test_native_wire_after_pending_turn_matches_continuous(monkeypatch, structured, checkpoint):
    continuous, reads, _ = scenario(monkeypatch, structured=structured, pause=False, checkpoint=checkpoint)
    restarted, restored_reads, operations = scenario(
        monkeypatch, structured=structured, pause=True, checkpoint=checkpoint
    )
    assert restarted == continuous
    assert restored_reads == reads
    assert len({operation for question, operation in operations if question == "waiting question"}) == 1
    if structured:
        assert sum(question == "ready question" for question, _ in operations) == 1
    else:
        assert [options["read_id"] for _, _, options in reads] == ["E1", "E2", "E3", "E4", "E5", "E7"]
    assert [int(body["metadata"]["podgen_source_turn"]) for body in restarted] == list(range(len(restarted)))
    if checkpoint:
        assert [t.get("name") for t in restarted[5]["tools"]] == ["save_working_state"]


@pytest.mark.parametrize("credit", [False, True])
def test_retained_short_checkpoint_and_credit_retry_keep_continuous_wire(monkeypatch, credit):
    from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
    from podgen.runtime.models.research_context import ResearchContext

    class LocalCrash(BaseException):
        pass

    def run(crash):
        journal, profile, limits, corpus = research_setup(monkeypatch)
        journal.root.update(invocation_id=UUID(int=201), run_id=UUID(int=202))
        journal.roots = {journal.root["invocation_id"]: journal.root}
        profile.context_limit = 200000
        from podgen.runtime.evaluation.source_tool_contract import bind

        journal.root["stage"] = "fact-review"
        body = bind(
            journal.request(journal.root), "fact-review", profile, journal.snapshot["source_tool_contract"]
        )
        journal.root["request_record_id"] = journal.store_request(body)
        sent = []
        failed = False

        def handler(request):
            nonlocal failed
            body = json.loads(request.content)
            if request.url.path.endswith("/input_tokens"):
                outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
                return httpx2.Response(200, json=dict(input_tokens=190000 if outputs else 100))
            sent.append(deepcopy(body))
            turn = int(body["metadata"]["podgen_source_turn"])
            if turn == 0:
                output = [call(1)]
            elif turn == 1:
                assert not any(i.get("type") == "function_call_output" for i in body["input"])
                if credit and not failed:
                    failed = True
                    return httpx2.Response(429, json=dict(error=dict(code="credit_balance_exhausted")))
                output = [
                    dict(
                        type="function_call",
                        status="completed",
                        name="save_working_state",
                        call_id="checkpoint",
                        arguments=json.dumps(
                            dict(state="Exact notes", resume_state="Finish", receipt_ids=["E1"])
                        ),
                    )
                ]
            else:
                output = [final()]
            return answer(body, turn, output)

        original = ResearchContext.publish
        raised = False

        def publish(self, *args, **kwargs):
            nonlocal raised
            if crash and not raised:
                raised = True
                raise LocalCrash()
            return original(self, *args, **kwargs)

        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:

            def provider(_):
                return OpenAIProvider("local", http_client=client)

            root = journal.root
            if credit:
                with pytest.raises(RunFailure, match="provider_http_429"):
                    invoke(ModelGateway(journal, provider, source_corpus=corpus), root, profile, limits)
                root = authorize_credit_retry(
                    journal, root["run_id"], root["invocation_id"], guard=lambda: None
                )
            with monkeypatch.context() as patch:
                from podgen.runtime.evaluation import model_turns

                events = model_turns.events

                def indexed_events(*args, **kwargs):
                    assert kwargs.get("kinds") != {"source_tool_result"}, (
                        "Retry reread the ancestor's full receipt bodies"
                    )
                    return events(*args, **kwargs)

                patch.setattr(model_turns, "events", indexed_events)
                patch.setattr(ResearchContext, "publish", publish)
                if crash:
                    with pytest.raises(LocalCrash):
                        invoke(ModelGateway(journal, provider, source_corpus=corpus), root, profile, limits)
                assert invoke(ModelGateway(journal, provider, source_corpus=corpus), root, profile, limits)
        return sent

    assert run(False) == run(True)


@pytest.mark.parametrize("fault", [None, "owner", "access", "integrity"])
def test_cursor_reads_only_the_retained_request_and_fails_closed(fault):
    from contextlib import nullcontext
    from uuid import uuid4

    from podgen.domain.errors import AccessDenied, Conflict, ContractError
    from podgen.runtime.models.research_resume import retained_turn

    root = dict(invocation_id=uuid4(), run_id=uuid4())
    latest = dict(
        invocation_id=uuid4(), turn_index=42, request_record_id=uuid4(), status="response_completed"
    )
    body = dict(metadata=dict(podgen_invocation_id=str(latest["invocation_id"]), podgen_source_turn="42"))
    if fault == "owner":
        body["metadata"]["podgen_source_turn"] = "41"
    seen, cached = [], []

    def read(reference):
        seen.append(reference.record_id)
        assert reference.record_id == latest["request_record_id"]
        if fault == "access":
            raise AccessDenied()
        return dict(
            request_blob=dict(
                storage_key="local", sha256="0" * 64, byte_length=1, media_type="application/json"
            )
        )

    def blob(*args):
        if fault == "integrity":
            raise ContractError("blob_integrity_error")
        return json.dumps(body).encode()

    journal = SimpleNamespace(
        read_operation=nullcontext,
        read=read,
        read_blob=blob,
        execution=SimpleNamespace(remember_physical=lambda *a: cached.append(a)),
    )
    history = SimpleNamespace(
        responses={0: None, 42: None},
        provider_response=lambda i: dict(
            invocation_id=str(root["invocation_id"]), data=dict(source_turn_id=str(latest["invocation_id"]))
        ),
    )
    ledger = SimpleNamespace(rows={0: dict(status="response_completed"), 42: latest})
    if fault:
        with pytest.raises((AccessDenied, Conflict, ContractError)):
            retained_turn(journal, root, ledger, history, minimum=0)
        assert not cached
    else:
        assert retained_turn(journal, root, ledger, history, minimum=0).index == 42
        assert len(cached) == 1
    assert seen == [latest["request_record_id"]]
