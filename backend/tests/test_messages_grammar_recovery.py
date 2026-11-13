"""Exact grammar-rejection recovery and native schema policy, with no provider network."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from podgen.runtime.evaluation.compiler import count_request, response_request
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.schema_refusal import MESSAGES, evidence
from podgen.runtime.evaluation.technical_recovery import authorize_technical_recovery, validate_retry
from podgen.runtime.evaluation.usage import attempt_usage
from podgen.runtime.models.anthropic_provider import AnthropicProvider
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.messages_tool_policy import KEY, NAVIGATION, VERSION
from podgen.runtime.models.messages_wire import messages_request
from tests.source_tool_runtime_support import MemoryJournal
from tests.test_research_navigation_contract import _profile
from tests.test_research_runtime import invoke, research_setup

GRAMMAR_ERROR = {"type": "error", "error": {"type": "invalid_request_error", "message": sorted(MESSAGES)[1]}}


def request(monkeypatch):
    journal, profile, limits, corpus = research_setup(monkeypatch)
    body = journal.request(journal.root)
    body["model"] = "claude-opus-5-5"
    body.pop("include", None)
    profile.provider = "anthropic"
    return journal, profile, limits, corpus, body


def test_new_anthropic_requests_pin_policy_and_preserve_it_for_count():
    profile = _profile("draft")
    from podgen.runtime.models.provider_contracts import contract, tool_policy

    manifest = dict(provider_contracts={"draft": contract(profile)})
    body = response_request(
        uuid4(),
        "draft",
        profile,
        {},
        "Pinned English prompt.",
        {"type": "object"},
        messages_tool_policy=tool_policy(manifest, "draft"),
    )
    assert body["metadata"][KEY] == VERSION
    assert count_request(body)["metadata"][KEY] == VERSION


def test_native_policy_only_changes_navigation_strictness(monkeypatch):
    _, _, _, _, body = request(monkeypatch)
    old = messages_request(body)
    frozen = deepcopy(body)
    body["metadata"][KEY] = VERSION
    new = messages_request(body)
    expected = deepcopy(old)
    for tool in expected["tools"]:
        if tool["name"] in NAVIGATION:
            tool["strict"] = False
    assert new == expected
    assert messages_request(frozen) == old
    assert sum(tool.get("strict") is True for tool in new["tools"]) == 3
    assert new["output_config"] == old["output_config"]
    assert messages_request(count_request(body), counting=True) == {
        key: value for key, value in new.items() if key not in {"stream", "max_tokens"}
    }
    body["metadata"][KEY] = "unknown"
    with pytest.raises(ProviderFault, match="unsupported_messages_tool_policy"):
        messages_request(body)


def rejected(monkeypatch):
    _, _, _, _, body = request(monkeypatch)
    journal = MemoryJournal({}, body)
    row = journal.root
    row.update(status="failed", input_tokens=100)
    journal.event(row, "submit_intent", data=dict(client_request_id=str(row["invocation_id"])))
    ref = journal.event(
        row,
        "provider_error",
        data=dict(
            operation="submit",
            ambiguous=False,
            code="provider_http_400",
        ),
        reply=WireReply(json.dumps(GRAMMAR_ERROR).encode(), 400),
    )
    record = journal.records[ref.record_id]
    record["wire_request_blob"] = journal.blob(json.dumps(messages_request(body)).encode()).model_dump(
        mode="json"
    )
    return journal, row, ref, record


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "ambiguous",
        "other_400",
        "wrong_status",
        "response_id",
        "output_usage",
        "request_changed",
        "missing_wire",
        "already_current",
        "no_navigation",
        "response_event",
        "duplicate_submit",
    ],
)
def test_only_exact_immutable_no_generation_evidence_is_eligible(monkeypatch, change):
    journal, row, ref, record = rejected(monkeypatch)
    if change == "ambiguous":
        record["data"]["ambiguous"] = True
    if change == "other_400":
        record["response_blob"] = journal.blob(
            json.dumps(
                dict(type="error", error=dict(type="invalid_request_error", message="Invalid max_tokens"))
            ).encode()
        ).model_dump(mode="json")
    if change == "wrong_status":
        record["provider_http_status"] = 500
    if change == "response_id":
        row["provider_response_id"] = "msg_paid"
    if change == "output_usage":
        row["output_tokens"] = 1
    if change == "request_changed":
        native = messages_request(journal.request(row))
        native["system"] = "Changed"
        record["wire_request_blob"] = journal.blob(json.dumps(native).encode()).model_dump(mode="json")
    if change == "missing_wire":
        record.pop("wire_request_blob")
    if change in {"already_current", "no_navigation"}:
        body = journal.request(row)
        if change == "already_current":
            body["metadata"][KEY] = VERSION
        else:
            body["tools"] = [tool for tool in body["tools"] if tool["name"] not in NAVIGATION]
        row["request_record_id"] = journal.store_request(body)
        for event in journal.events(row):
            journal.records[event["record_id"]]["request_ref"]["record_id"] = str(row["request_record_id"])
        record["wire_request_blob"] = journal.blob(json.dumps(messages_request(body)).encode()).model_dump(
            mode="json"
        )
    if change == "response_event":
        journal.event(row, "provider_response", data={})
    if change == "duplicate_submit":
        journal.event(row, "submit_intent", data=dict(client_request_id=str(row["invocation_id"])))
    assert (evidence(journal, row) is not None) == (change == "valid")


def test_real_database_retry_keeps_paid_turn_and_validates_non_strict_arguments(repo, scope, monkeypatch):
    local = pytest.MonkeyPatch()
    memory, profile, limits, corpus, body = request(local)
    local.undo()
    corpus._materials = SimpleNamespace(runtime_admissions=[])
    corpus.refresh = lambda: None
    journal = EvaluationJournal(repo, scope[0])
    profile_ref = create_profile(
        repo, scope[0], model=body["model"], provider="anthropic", context_limit=100000
    ).ref
    author = journal.record(dict(contract="grammar-test-material/1"), kind="user_input")
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id="grammar",
        author_ref=author,
        writer_profile_ref=profile_ref,
        reviewer_profile_ref=profile_ref,
    )
    journal.create_run(command, dict(memory.snapshot, stages=["draft"], command=command_payload(command)))
    root = journal.prepare(
        command.run_id, "draft", body, dict(profile_ref=profile_ref.model_dump(mode="json"))
    )
    sent = []

    def handler(req):
        native = json.loads(req.content)
        if req.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        sent.append(native)
        number = len(sent)
        if number == 2:
            return httpx.Response(400, json=GRAMMAR_ERROR)
        content = (
            [
                dict(
                    type="tool_use",
                    id=f"tool_{number}",
                    name="read_source",
                    input=dict(source_id="S1", start=0 if number == 1 else "invalid", end=4000),
                )
            ]
            if number < 4
            else [dict(type="text", text='{"answer":"done"}')]
        )
        return httpx.Response(
            200,
            json=dict(
                type="message",
                role="assistant",
                model=body["model"],
                id=f"msg_{number}",
                stop_reason="tool_use" if number < 4 else "end_turn",
                content=content,
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal,
            lambda _: AnthropicProvider("test-key-absent-from-content", http_client=client),
            source_corpus=corpus,
        )
        with pytest.raises(RunFailure, match="provider_http_400"):
            invoke(gateway, root, profile, limits)
        parent = journal.invocation(command.run_id, "draft")
        from podgen.runtime.evaluation.source_tool_recovery import retry_request

        old_body = journal.request(parent)
        failed_body = retry_request(journal, parent)
        old_events = journal.events(parent)
        paid = attempt_usage(journal, parent, profile.output_limit)
        assert paid == dict(input=100, output=10)
        child = authorize_technical_recovery(
            journal,
            command,
            parent["invocation_id"],
            gateway=gateway,
            load_profile=lambda ref, stage: profile,
            guard=lambda: None,
        )
        validate_retry(journal, child, parent)
        assert journal.request(child)["metadata"][KEY] == VERSION
        assert {k: v for k, v in journal.request(child).items() if k != "metadata"} == {
            k: v for k, v in failed_body.items() if k != "metadata"
        }
        assert invoke(gateway, child, profile, limits, totals=paid) == '{"answer":"done"}'
        assert journal.request(parent) == old_body and journal.events(parent) == old_events
        assert len(sent) == 4 and len(corpus.calls) == 1
        assert all(tool.get("strict") is True for tool in sent[1]["tools"])
        assert sum(tool.get("strict") is True for tool in sent[2]["tools"]) == 3
        assert all(native["output_config"]["format"] == sent[0]["output_config"]["format"] for native in sent)
        assert "invalid_source_tool_arguments" in json.dumps(sent[3]["messages"])
        assert sum("Full exact source passage" in json.dumps(native["messages"]) for native in sent[1:]) == 3
        before = len(sent)
        assert (
            invoke(gateway, journal.invocation(command.run_id, "draft"), profile, limits, totals=paid)
            == '{"answer":"done"}'
        )
        assert len(sent) == before


def test_old_manifest_snapshot_keeps_legacy_policy_and_new_preview_uses_current():
    from podgen.runtime.evaluation.preview import preview_request
    from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
    from podgen.runtime.evaluation.source_tool_contract import create
    from podgen.runtime.models.provider_contracts import TOOL_POLICY, contract
    from podgen.writing.author_reply import contract_pin
    from podgen.writing.model_references import CATALOG_KEY, preview_references
    from podgen.writing.projection_versions import V9
    from podgen.writing.research_materials.pinning import pin
    from podgen.writing.roles import WriteOutput
    from tests.test_research_navigation_contract import _plan, research_materials

    materials = research_materials.__wrapped__()
    profile = _profile("draft")
    run_id = uuid4()
    provider_contract = contract(profile)
    provider_contract.pop(TOOL_POLICY)
    manifest = dict(
        behavior_profile="production/1",
        author_plan_handoff="approved-story/1",
        role_projection_version=V9,
        response_schemas={"draft": WriteOutput.model_json_schema()},
        provider_contracts={"draft": provider_contract},
        source_tool_contract=create(SimpleNamespace(name="production/1"), materials),
        research_material_contract=pin(materials),
    )
    manifest[CATALOG_KEY] = preview_references(materials, V9).as_payload()
    manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
    snapshot = PreviewSnapshot(manifest, "Pinned English literary instructions.")
    legacy = preview_request(
        run_id, "draft", profile, materials, snapshot=snapshot, approved_plan=_plan(materials)
    )
    assert KEY not in legacy["metadata"]
    assert all(tool.get("strict") is True for tool in messages_request(legacy)["tools"])
    current_manifest = deepcopy(manifest)
    current_manifest["provider_contracts"]["draft"][TOOL_POLICY] = VERSION
    current = preview_request(
        run_id,
        "draft",
        profile,
        materials,
        snapshot=PreviewSnapshot(current_manifest, snapshot.instructions),
        approved_plan=_plan(materials),
    )
    expected = deepcopy(legacy)
    expected["metadata"][KEY] = VERSION
    assert current == expected
    future = preview_request(run_id, "draft", profile, materials, approved_plan=_plan(materials))
    assert future["metadata"][KEY] == VERSION
