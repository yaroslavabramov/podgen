"""A repaired source review retains nine paid turns through a proven credit recovery."""

import json
from copy import deepcopy
from uuid import uuid4

import httpx2
import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.recheck_request import semantic_request
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.review_repair import apply_anchor_repair, prepare_anchor_repair
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.fact_review import FactFeedback
from podgen.writing.projections import CONTRACT, indexed_draft
from tests.recheck_repair_cases import RepairJournal, ref
from tests.test_source_tool_runtime import call, setup


class RecoveryRepairJournal(RepairJournal):
    def prepare(self, *args, **kwargs):
        row = super().prepare(*args, **kwargs)
        row.update(input_tokens=None, output_tokens=None)
        if "authorization" in kwargs:
            self.records[row["request_record_id"]]["authorization"] = deepcopy(kwargs["authorization"])
        return row


def recovered_repair(monkeypatch):
    seed, profile, limits, corpus = setup(monkeypatch, turns=12)
    journal = RecoveryRepairJournal(dict(seed.snapshot, input_contract=CONTRACT), {})
    root = journal.root
    root.update(stage="fact-review", parent_attempt_index=None)
    script = dict(utterances=[dict(speaker="Host", text="Exact original fact.")])
    rejected = dict(
        reading_mode="informed",
        listener_answer="The contextual account.",
        important_limits=[],
        strengths=[],
        findings=[
            dict(
                dimension="factuality",
                priority="important",
                anchors=[dict(utterance_index=99, quote="Exact original fact.")],
                observation="A scoped issue.",
                requested_change="Preserve the factual limit.",
                preserve=[],
                support_labels=[],
            )
        ],
        research_requests=[],
        duration_note="Not measured.",
        recommendation="revise",
    )
    body = deepcopy(seed.request(seed.root))
    body["metadata"] = dict(
        podgen_run_id=str(root["run_id"]),
        podgen_stage=root["stage"],
        podgen_invocation_id=str(root["invocation_id"]),
    )
    body["input"][0]["content"][0]["text"] = json.dumps(dict(script=indexed_draft(script)))
    body["text"]["format"].update(name="fact_review", schema=FactFeedback.model_json_schema())
    root["request_record_id"] = journal.store_request(body)
    script_ref = ref(uuid4())
    context = dict(role_input_contract=CONTRACT, script_ref=script_ref, script_hash=digest(script))
    journal.records[root["request_record_id"]].update(
        contract="model-request-v1",
        run_id=str(root["run_id"]),
        stage=root["stage"],
        invocation_id=str(root["invocation_id"]),
        operation_kind="role",
        attempt_kind="role",
        parent_request_ref=None,
        context=context,
    )
    sent = []
    refused = False

    def handler(request):
        nonlocal refused
        payload = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        sent.append(payload)
        turn = int(payload["metadata"]["podgen_source_turn"])
        if turn == 9 and not refused:
            refused = True
            return httpx2.Response(429, json=dict(error=dict(code="credit_balance_exhausted")))
        output = (
            [call(turn + 1)]
            if turn < 9
            else [
                dict(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[dict(type="output_text", text=json.dumps(rejected))],
                )
            ]
        )
        return httpx2.Response(
            200,
            json=dict(
                id=f"resp_{len(sent)}",
                model=payload["model"],
                metadata=payload["metadata"],
                status="completed",
                output=output,
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )

        def invoke(row):
            return gateway.invoke(
                row.copy(), profile, limits=limits, totals=dict(input=0, output=0), guard=lambda: None
            )

        with pytest.raises(RunFailure):
            invoke(root)
        retry = authorize_credit_retry(journal, root["run_id"], root["invocation_id"], guard=lambda: None)
        assert json.loads(invoke(retry)) == rejected
    assert [int(x["metadata"]["podgen_source_turn"]) for x in sent] == [*range(10), 9]
    assert len(corpus.calls) == 9
    retry = journal.transition(retry, "validation_failed")
    leaf = prepare_anchor_repair(journal, retry, draft=script, draft_ref=script_ref, guard=lambda: None)
    offered = request_payload(journal.request(leaf))
    choices = dict(
        choices=[
            dict(anchor_id=x["anchor_id"], selector_id=x["selector_ids"][0])
            for x in offered["invalid_anchors"]
        ]
    )
    fixed, _ = apply_anchor_repair(journal, leaf, json.dumps(choices), draft=script)
    leaf.update(status="completed", output_record_id=uuid4())
    journal.records[leaf["output_record_id"]] = dict(
        request_ref=ref(leaf["request_record_id"]), output=fixed, repair_choices=choices
    )
    return journal, root, retry, leaf, script, fixed


def verify(case):
    journal, root, _, leaf, script, output = case
    context = journal.records[leaf["request_record_id"]]["context"]
    return semantic_request(journal, root["run_id"], root["stage"], leaf, context, script, output)


def test_repaired_credit_recovery_replays_exact_paid_conversation(monkeypatch):
    case = recovered_repair(monkeypatch)
    journal, root, retry, *_ = case
    before = deepcopy(journal.records)
    assert verify(case) == journal.request(retry)
    assert request_payload(verify(case)) == request_payload(journal.request(root))
    assert journal.records == before


@pytest.mark.parametrize("wrong", ["tool-result", "input", "authorization", "parent", "context"])
def test_changed_source_retry_or_crossed_chain_is_rejected(monkeypatch, wrong):
    case = recovered_repair(monkeypatch)
    journal, root, retry, leaf, *_ = case
    record = journal.records[retry["request_record_id"]]
    if wrong in ("tool-result", "input"):
        body = journal.request(retry)
        if wrong == "tool-result":
            body["input"][-1]["output"] = '{"passage":"Changed evidence"}'
        else:
            body["input"][0]["content"][0]["text"] = '{"script":"Changed assignment"}'
        record["request_blob"] = journal.blob(json.dumps(body).encode()).model_dump(mode="json")
    elif wrong == "authorization":
        record["authorization"]["source_tool_resume"]["turn_index"] = 8
    elif wrong == "parent":
        journal.roots[retry["invocation_id"]]["parent_invocation_id"] = uuid4()
    else:
        record["context"]["script_hash"] = "changed"
    with pytest.raises((Conflict, RunFailure)):
        verify(case)
