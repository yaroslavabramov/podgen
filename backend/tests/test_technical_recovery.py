"""Real gateway recovery with an in-memory durable boundary; no provider or database calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.technical_recovery import (
    authorize_technical_recovery,
    resume_current,
    validate_retry,
)
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.source_tool_runtime_support import MemoryJournal
from tests.test_source_tool_recovery import invoke, prepare
from tests.test_source_tool_runtime import call, final, setup


class Journal(MemoryJournal):
    def last_response(self, row):
        for event in reversed(self.events(row)):
            if event["event_kind"] == "provider_response":
                ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
                return self.read_blob(ref, BlobRef.model_validate(self.read(ref)["response_blob"]))
        return None


class Provider:
    def __init__(self, faults=1):
        self.sent, self.reads, self.faults = [], [], faults
        self.retrieve_error = None
        self.response = None

    def count(self, body):
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.sent.append(deepcopy(body))
        self.response = dict(
            id="resp_known",
            model=body["model"],
            metadata=body["metadata"],
            status="completed",
            output=[final()],
            usage=dict(input_tokens=100, output_tokens=10),
        )
        if self.faults:
            self.faults -= 1
            raise ProviderFault("provider_transport_outcome_unknown", ambiguous=True)
        return WireReply(json.dumps(self.response).encode())

    def retrieve(self, response_id):
        self.reads.append(response_id)
        if self.retrieve_error:
            raise ProviderFault(self.retrieve_error)
        return WireReply(json.dumps(self.response).encode())


def plain(faults=1):
    body = dict(
        model="test", instructions="Pinned instructions", input=[], metadata=dict(test="same"), store=False
    )
    journal = Journal(dict(stages=["draft"]), body)
    journal.records[journal.root["request_record_id"]]["context"] = dict(profile_ref={"saved": "profile"})
    provider = Provider(faults)
    profile = SimpleNamespace(
        output_limit=1000, context_limit=10000, generation_parameters=dict(store=False), timeout_policy={}
    )
    limits = SimpleNamespace(max_total_input_tokens=10000, max_total_output_tokens=10000)
    gateway = ModelGateway(journal, lambda _: provider)
    return journal, provider, profile, limits, gateway


def recover(journal, gateway, profile, row, **kwargs):
    return authorize_technical_recovery(
        journal,
        SimpleNamespace(run_id=row["run_id"]),
        row["invocation_id"],
        gateway=gateway,
        load_profile=lambda ref, stage: profile,
        guard=lambda: None,
        **kwargs,
    )


def test_linked_replacement_retains_unknown_original_exact_inputs_and_budget():
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    parent = journal.root.copy()
    saved = deepcopy(journal.request(parent))
    original_events = journal.events(parent)
    paid = attempt_usage(journal, parent, profile.output_limit)
    child = recover(journal, gateway, profile, parent)
    assert recover(journal, gateway, profile, parent) == child
    assert parent["status"] == "outcome_unknown" and journal.root == parent
    assert journal.events(parent) == original_events and journal.request(parent) == saved
    assert paid == dict(input=100, output=1000)
    assert invoke(gateway, child, profile, limits, paid) == '{"answer":"done"}'
    assert len(provider.sent) == 2 and not provider.reads
    assert {k: v for k, v in provider.sent[0].items() if k != "metadata"} == {
        k: v for k, v in provider.sent[1].items() if k != "metadata"
    }
    assert provider.sent[0]["metadata"] != provider.sent[1]["metadata"]
    assert attempt_usage(journal, parent, profile.output_limit) == paid


def test_unknown_reservation_can_block_new_submit():
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    child = recover(journal, gateway, profile, journal.root.copy())
    limits.max_total_output_tokens = 1500
    with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
        invoke(gateway, child, profile, limits, dict(input=100, output=1000))
    assert len(provider.sent) == 1


def test_stage_limit_does_not_reset_with_new_targets():
    journal, provider, profile, limits, gateway = plain(faults=4)
    row = journal.root.copy()
    for index in range(3):
        with pytest.raises(RunFailure):
            invoke(gateway, row, profile, limits)
        row = journal.roots[row["invocation_id"]].copy()
        if index < 2:
            row = recover(journal, gateway, profile, row)
    with pytest.raises(RunFailure, match="technical_recovery_limit_exhausted"):
        recover(journal, gateway, profile, row)
    assert len(journal.roots) == len(provider.sent) == 3


@pytest.mark.parametrize("error", [None, "provider_http_404", "provider_http_503"])
def test_known_response_is_retrieved_before_any_replacement(error):
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    journal.transition(journal.root, "outcome_unknown", provider_id="resp_known")
    parent = journal.root.copy()
    provider.retrieve_error = error
    if error == "provider_http_503":
        with pytest.raises(RunFailure, match=error):
            recover(journal, gateway, profile, parent)
        assert len(journal.roots) == 1 and journal.root["status"] == "outcome_unknown"
    else:
        row = recover(journal, gateway, profile, parent)
        if error is None:
            assert row["invocation_id"] == parent["invocation_id"]
            assert row["status"] == "response_completed"
            assert recover(journal, gateway, profile, row) == row
            assert invoke(gateway, row, profile, limits) == '{"answer":"done"}'
            assert len(journal.roots) == 1
        else:
            assert row["attempt_kind"] == "technical_retry"
            assert journal.root["status"] == "outcome_unknown"
    assert provider.reads == ["resp_known"] and len(provider.sent) == 1


def test_retrieved_wrong_response_cannot_release_reservation_or_create_retry():
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    journal.transition(journal.root, "outcome_unknown", provider_id="resp_known")
    provider.response["metadata"] = {"other": "request"}
    with pytest.raises(RunFailure, match="provider_response_metadata_mismatch"):
        recover(journal, gateway, profile, journal.root.copy())
    assert journal.root["status"] == "outcome_unknown" and len(journal.roots) == 1


def test_changed_request_or_authorization_cannot_submit():
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    parent = journal.root.copy()
    child = recover(journal, gateway, profile, parent)
    journal.records[child["request_record_id"]]["authorization"]["maximum_stage_replacements"] = 9
    with pytest.raises(Conflict, match="technical_recovery_authorization_mismatch"):
        validate_retry(journal, child, parent)
    assert len(provider.sent) == 1


@pytest.mark.parametrize("fault", ["timeout", "520"])
def test_source_retry_keeps_completed_reads_transcript_and_unknown_turn_cost(monkeypatch, fault):
    import httpx2 as httpx

    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "unknown")
    journal.snapshot["stages"] = [journal.root["stage"]]
    journal.records[journal.root["request_record_id"]]["context"] = dict(profile_ref={"saved": "profile"})
    original_handler = handler

    def handler(request):
        try:
            return original_handler(request)
        except httpx.ReadTimeout:
            if fault == "timeout":
                raise
            return httpx.Response(520, json={"error": {"message": "Upstream disconnected"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        parent = journal.root.copy()
        paid = attempt_usage(journal, parent, profile.output_limit)
        child = recover(journal, gateway, profile, parent)
        assert invoke(gateway, child, profile, limits, paid) == '{"answer":"done"}'
    assert [int(body["metadata"]["podgen_source_turn"]) for body in server.sent] == [0, 1, 1, 2]
    assert server.sent[1]["input"] == server.sent[2]["input"]
    assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2"]
    assert paid == attempt_usage(journal, parent, profile.output_limit) == dict(input=450, output=24586)
    assert journal.roots[parent["invocation_id"]]["status"] == "outcome_unknown"


def test_ordinary_recovery_skips_absent_optional_stages_and_reuses_existing_child():
    journal, provider, profile, limits, gateway = plain()
    journal.snapshot["stages"] = ["plan-revise", "plan-review-revised", "draft", "reader-review"]
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    command = SimpleNamespace(run_id=journal.root["run_id"])
    child = resume_current(
        journal, command, gateway=gateway, load_profile=lambda ref, stage: profile, guard=lambda: None
    )
    assert child["attempt_kind"] == "technical_retry"
    assert (
        resume_current(
            journal, command, gateway=gateway, load_profile=lambda ref, stage: profile, guard=lambda: None
        )
        is None
    )
    assert len(journal.roots) == 2 and len(provider.sent) == 1


@pytest.mark.parametrize("known_id", [False, True])
def test_completed_archive_is_reused_after_lost_projection_update_without_provider_access(known_id):
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    journal.event(journal.root, "provider_response", reply=WireReply(json.dumps(provider.response).encode()))
    if known_id:
        journal.transition(journal.root, "outcome_unknown", provider_id="resp_known")
    provider.retrieve_error = "provider_http_503"
    gateway.provider_factory = lambda _: pytest.fail("A complete archive must not require a provider")
    row = recover(journal, gateway, profile, journal.root.copy())
    assert row["status"] == "response_completed"
    assert invoke(gateway, row, profile, limits) == '{"answer":"done"}'
    assert len(provider.sent) == len(journal.roots) == 1 and not provider.reads


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", "resp_another", "provider_response_id_mismatch"),
        ("model", "other-model", "provider_model_revision_mismatch"),
        ("metadata", {"other": "operation"}, "provider_response_metadata_mismatch"),
    ],
)
def test_wrong_local_completion_is_rejected_without_provider_or_releasing_reservation(field, value, code):
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    journal.transition(journal.root, "outcome_unknown", provider_id="resp_known")
    response = dict(provider.response, **{field: value})
    journal.event(journal.root, "provider_response", reply=WireReply(json.dumps(response).encode()))
    paid = attempt_usage(journal, journal.root, profile.output_limit)
    gateway.provider_factory = lambda _: pytest.fail("Invalid local bindings must be rejected locally")
    with pytest.raises(RunFailure, match=code):
        recover(journal, gateway, profile, journal.root.copy())
    assert attempt_usage(journal, journal.root, profile.output_limit) == paid == dict(input=100, output=1000)
    assert journal.root["status"] == "outcome_unknown" and len(journal.roots) == 1
    assert not provider.reads and len(provider.sent) == 1


@pytest.mark.parametrize("archived", [b'{"status":"in_progress"}', b"{broken-json"])
def test_incomplete_local_archive_still_retrieves_known_response(archived):
    journal, provider, profile, limits, gateway = plain()
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    journal.transition(journal.root, "outcome_unknown", provider_id="resp_known")
    journal.event(journal.root, "provider_response", reply=WireReply(archived))
    restored = recover(journal, gateway, profile, journal.root.copy())
    assert restored["status"] == "response_completed"
    assert provider.reads == ["resp_known"] and len(provider.sent) == len(journal.roots) == 1


@pytest.mark.parametrize("archived", [False, True])
def test_known_completed_tool_response_reconciles_in_place_without_repeating_reads(monkeypatch, archived):
    import httpx2 as httpx

    journal, profile, limits, corpus, server, original = prepare(monkeypatch, "unknown")
    journal.snapshot["stages"] = [journal.root["stage"]]
    journal.records[journal.root["request_record_id"]]["context"] = dict(profile_ref={"saved": "profile"})
    known = None
    reads = []

    def handler(request):
        nonlocal known
        if request.method == "GET":
            reads.append(str(request.url))
            if archived:
                return httpx.Response(503, json={"error": {"message": "Unavailable"}})
            return httpx.Response(200, json=dict(known, status="completed", output=[call(2)]))
        body = json.loads(request.content)
        if (
            request.url.path.endswith("/responses")
            and body["metadata"]["podgen_source_turn"] == "1"
            and known is None
        ):
            known = dict(
                id="resp_recoverable",
                model=body["model"],
                metadata=body["metadata"],
                status="in_progress",
                output=[],
                usage=dict(input_tokens=350, output_tokens=10),
            )
            server.sent.append(body)
            return httpx.Response(200, json=known)
        return original(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="response_not_retrievable"):
            invoke(gateway, journal.root, profile, limits)
        if archived:
            turn = model_turns.rows(journal, journal.root)[-1]
            model_turns.TurnJournal(journal, journal.root, turn["turn_index"]).event(
                turn,
                "provider_response",
                reply=WireReply(json.dumps(dict(known, status="completed", output=[call(2)])).encode()),
                data=dict(provider_response_id=known["id"]),
            )
            factory = gateway.provider_factory
            gateway.provider_factory = lambda _: pytest.fail("A completed tool turn is available locally")
        restored = recover(journal, gateway, profile, journal.root.copy())
        if archived:
            gateway.provider_factory = factory
        assert restored["status"] == "tool_running"
        assert invoke(gateway, restored, profile, limits) == '{"answer":"done"}'
    assert len(journal.roots) == 1 and len(reads) == (0 if archived else 1)
    assert [int(body["metadata"]["podgen_source_turn"]) for body in server.sent] == [0, 1, 2]
    assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2"]


@pytest.mark.parametrize("state", ["validation_failed", "completed", "failed"])
def test_semantic_failure_or_finished_response_never_creates_technical_replacement(state):
    journal, provider, profile, limits, gateway = plain(faults=0)
    invoke(gateway, journal.root, profile, limits)
    journal.transition(journal.root, state)
    with pytest.raises(RunFailure, match="technical_recovery_not_eligible"):
        recover(journal, gateway, profile, journal.root.copy())
    assert len(journal.roots) == len(provider.sent) == 1


def test_failed_fourth_count_resumes_exact_tool_transcript_without_repeating_three_reads(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch, turns=5)
    journal.snapshot["stages"] = [journal.root["stage"]]
    journal.records[journal.root["request_record_id"]]["context"] = dict(profile_ref={"saved": "profile"})
    counts, sent = [], []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            counts.append(body)
            if len(counts) == 4:
                raise httpx.ReadTimeout("Count endpoint interrupted", request=request)
            return httpx.Response(200, json=dict(input_tokens=100))
        sent.append(body)
        turn = int(body["metadata"]["podgen_source_turn"])
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_turn_{turn}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call(turn + 1)] if turn < 3 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure, match="provider_transport_error"):
            invoke(gateway, journal.root, profile, limits)
        parent = journal.root.copy()
        assert parent["status"] == "failed"
        assert len(sent) == len(corpus.calls) == 3
        original = model_turns.rows(journal, parent)[-1]
        assert original["turn_index"] == 3 and original["input_tokens"] is None
        paid = attempt_usage(journal, parent, profile.output_limit)
        assert paid == dict(input=300, output=30)
        row = resume_current(
            journal,
            SimpleNamespace(run_id=parent["run_id"]),
            gateway=gateway,
            load_profile=lambda ref, stage: profile,
            guard=lambda: None,
        )
        assert row["attempt_kind"] == "technical_retry"
        assert recover(journal, gateway, profile, parent) == row
        assert journal.request(row)["input"] == journal.request(original)["input"]
        assert invoke(gateway, row, profile, limits, paid) == '{"answer":"done"}'
    assert [body["metadata"]["podgen_source_turn"] for body in sent] == ["0", "1", "2", "3"]
    assert [options["read_id"] for _, _, options in corpus.calls] == ["E1", "E2", "E3"]
    assert counts[3] == counts[4]
    assert journal.roots[parent["invocation_id"]] == parent
    assert attempt_usage(journal, parent, profile.output_limit) == paid


def test_plain_count_failure_has_no_generation_reservation_and_recovers():
    journal, provider, profile, limits, gateway = plain(faults=0)
    original_count = provider.count
    provider.count = lambda _: (_ for _ in ()).throw(ProviderFault("provider_transport_error"))
    with pytest.raises(RunFailure, match="provider_transport_error"):
        invoke(gateway, journal.root, profile, limits)
    parent = journal.root.copy()
    assert not provider.sent
    assert attempt_usage(journal, parent, profile.output_limit) == dict(input=0, output=0)
    provider.count = original_count
    child = recover(journal, gateway, profile, parent)
    assert invoke(gateway, child, profile, limits) == '{"answer":"done"}'
    assert len(provider.sent) == 1


@pytest.mark.parametrize(
    "tamper", ["submit", "binding", "body", "configuration", "400", "validation", "budget"]
)
def test_count_recovery_requires_proof_of_unsent_transient_failure(tamper):
    journal, provider, profile, limits, gateway = plain()
    provider.count = lambda _: (_ for _ in ()).throw(ProviderFault("provider_transport_error"))
    with pytest.raises(RunFailure):
        invoke(gateway, journal.root, profile, limits)
    if tamper in ("validation", "budget"):
        journal.transition(journal.root, "validation_failed")
        journal.event(
            journal.root,
            "failure",
            data=dict(
                code="run_token_budget_exceeded" if tamper == "budget" else "role_output_validation_failed"
            ),
        )
    elif tamper == "submit":
        journal.event(
            journal.root, "submit_intent", data=dict(client_request_id=str(journal.root["invocation_id"]))
        )
    else:
        event = next(
            e
            for e in journal.events(journal.root)
            if e["event_kind"] == ("count_intent" if tamper == "body" else "provider_error")
        )
        record = journal.records[event["record_id"]]
        if tamper == "binding":
            record["invocation_id"] = "another-invocation"
        elif tamper == "body":
            record["data"]["request_blob"] = journal.blob(b"{}").model_dump(mode="json")
        else:
            record["data"]["code"] = "provider_http_400" if tamper == "400" else "provider_http_401"
    with pytest.raises((RunFailure, Conflict)):
        recover(journal, gateway, profile, journal.root.copy())
    assert len(journal.roots) == 1 and not provider.sent
