"""Evidence aliases retain actual requests, frozen selectors and reusable author passages."""

from copy import deepcopy
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.reading_audit import evidence
from podgen.runtime.evaluation.reading_audit.lifecycle import _locator
from podgen.writing.revision.assignment import tasks
from podgen.writing.source_tools import SourceCorpus, attach_task_passages

from tests.test_frozen_source_tools import finding, materials, read


def catalog(monkeypatch):
    m = materials()
    m.documents[0].ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    corpus = SourceCorpus(m)
    passage = read(corpus, 9)["passage"]
    root = dict(invocation_id=uuid4(), request_record_id=uuid4(), operation_kind="role")
    turn = dict(
        turn_index=0,
        status="response_completed",
        request_record_id=uuid4(),
        invocation_id=uuid4(),
        provider_response_id="resp_read",
    )
    ref = RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]).model_dump(
        mode="json"
    )
    event = dict(
        _record_id=uuid4(),
        invocation_id=str(root["invocation_id"]),
        request_ref=ref,
        data=dict(name="read_source", turn_index=0, result=dict(passage=passage)),
    )
    payload = dict(
        evidence=[dict(source="Book", text="Old card.", supports=[])],
        audit_context=dict(packet_evidence_index=[dict(evidence_id="B1", path="/evidence/0")]),
    )
    journal = NS(read=lambda ref: dict(context={}), request=lambda row: payload)
    monkeypatch.setattr(evidence, "request_payload", lambda body: body)
    monkeypatch.setattr(evidence, "lineage", lambda journal, root: [root])
    monkeypatch.setattr(evidence.model_turns, "rows", lambda journal, root: [turn])
    monkeypatch.setattr(evidence.model_turns, "events", lambda *a, **k: [event])
    index = {row["key"]: row for row in evidence.snapshots(m)}
    return m, corpus, root, turn, event, journal, index


def test_actual_packet_and_read_have_distinct_provenance_and_previous_alias(monkeypatch):
    m, corpus, root, turn, event, journal, index = catalog(monkeypatch)
    current = evidence.current_catalog(journal, root, m, index, corpus)
    assert current["E1"]["request_ref"]["record_id"] == str(turn["request_record_id"])
    assert current["E1"]["root_request_ref"]["record_id"] == str(root["request_record_id"])
    assert current["E1"]["provider_response_id"] == "resp_read"
    assert current["B1"]["content"]["text"] == "Old card."
    output = dict(
        audit=dict(
            claim_checks=[
                dict(
                    source_refs=[
                        dict(from_review="current", evidence_id="B1"),
                        dict(from_review="previous", evidence_id="H1"),
                    ]
                )
            ]
        )
    )
    resolved = evidence.resolve(output, current, dict(H1=current["E1"]), m, index)
    assert resolved["previous:H1"]["original_id"] == "E1"
    assert resolved["current:B1"]["content"] != resolved["previous:H1"]["content"]
    output["audit"]["claim_checks"][0]["source_refs"][0]["evidence_id"] = "invented"
    with pytest.raises(ContractError, match="audit_evidence_not_delivered"):
        evidence.resolve(output, current, {}, m, index)


@pytest.mark.parametrize("change", ["text", "sha256", "ref", "range", "hash"])
def test_changed_retained_evidence_is_rejected(monkeypatch, change):
    m, corpus, root, _, _, journal, index = catalog(monkeypatch)
    value = deepcopy(evidence.current_catalog(journal, root, m, index, corpus)["E1"])
    if change == "text":
        value["content"]["text"] = "Invented"
    elif change in ("sha256", "ref"):
        value["source_selectors"][0][change] = "changed"
    elif change == "range":
        value["source_selectors"][0]["start"] += 1
    else:
        value["content_hash"] = "changed"
    with pytest.raises(ContractError, match="audit_evidence_"):
        evidence.validate_retained(value, m, index)


def test_old_read_reaches_author_without_executing_source_tools(monkeypatch):
    m, corpus, root, _, _, journal, index = catalog(monkeypatch)
    value = evidence.current_catalog(journal, root, m, index, corpus)["E1"]
    monkeypatch.setattr(SourceCorpus, "execute", lambda *a, **k: pytest.fail("retained read was reexecuted"))
    locator = _locator(value, m, index, corpus)
    payload = dict(
        revision_tasks=tasks(dict(reader=dict(findings=[]), fact=dict(findings=[finding([locator])])), [])
    )
    attach_task_passages(payload, m)
    assert payload["review_source_passages"][0]["text"] == value["content"]["text"]
    assert payload["revision_tasks"][0]["source_read_refs"] == ["P1"]


def test_read_receipt_requires_its_actual_completed_turn(monkeypatch):
    m, corpus, root, turn, event, journal, index = catalog(monkeypatch)
    turn["status"] = "prepared"
    with pytest.raises(ContractError, match="audit_source_read_provenance_mismatch"):
        evidence.current_catalog(journal, root, m, index, corpus)
    turn["status"] = "response_completed"
    event["invocation_id"] = str(uuid4())
    with pytest.raises(ContractError, match="audit_source_read_provenance_mismatch"):
        evidence.current_catalog(journal, root, m, index, corpus)
