import json
from uuid import uuid4

import pytest

from podgen.domain.errors import Conflict, ContractError
from podgen.research.hosted.contracts import Approval
from podgen.research.hosted.export import export
from podgen.research.hosted.material import report
from podgen.research.hosted.service import advance, approve, prepare
from podgen.research.hosted.state import status
from podgen.research.hosted.transport import Response
from tests.hosted_research_support import Provider, ready, response


def test_approval_frozen_input_and_no_silent_truncation(monkeypatch):
    journal, config = ready(monkeypatch, approved=False)
    assert "x" * 10000 in journal.get("manifest")["request"]["body"]["input"]
    assert prepare(journal, config) == journal.get("manifest")
    with pytest.raises(Conflict):
        prepare(journal, config.model_copy(update={"instructions": "Changed"}))
    provider = Provider()
    with pytest.raises(ContractError, match="requires_approval"):
        advance(journal, provider)
    assert not provider.calls
    with pytest.raises(ContractError, match="request_mismatch"):
        approve(
            journal, Approval(request_hash="wrong", reason="Approval", accepts_variable_provider_cost=True)
        )


@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError("lost acknowledgement"),
        Response(503, {}, b"error"),
        Response(200, {}, b'{"id":', False, "ReadTimeout"),
        Response(200, {}, b"{}"),
    ],
)
def test_unknown_submission_never_submits_again(monkeypatch, reply):
    journal, _ = ready(monkeypatch)
    provider = Provider(reply)
    try:
        advance(journal, provider)
    except RuntimeError:
        pass
    assert advance(journal, provider)["state"] == "outcome_unknown"
    assert [name for name, _ in provider.calls] == ["submit"]


def test_poll_error_keeps_job_and_report_is_lossless(monkeypatch, tmp_path):
    journal, _ = ready(monkeypatch)
    full_text = "История часов. " * 12000
    native = dict(type="url_citation", url="https://example.org/source", start_index=0, end_index=28)
    completed = response(
        "completed",
        steps=[
            dict(type="thought", content="not report"),
            dict(type="model_output", content=[dict(type="text", text="interim")]),
            dict(
                type="model_output",
                content=[
                    dict(type="text", text=full_text, annotations=[native]),
                    dict(type="text", text="https://example.org/book_(edition)."),
                ],
            ),
        ],
        usage={"total_input_tokens": 42},
    )
    provider = Provider(response(), Response(503, {}, b"poll failed"), completed)
    assert advance(journal, provider)["state"] == "in_progress"
    failed = advance(journal, provider)
    assert failed["provider_job_id"] == "job/one" and failed["operation_error"] == "http_503"
    assert advance(journal, provider)["state"] == "completed"
    assert advance(journal, provider)["state"] == "completed"
    assert [name for name, _ in provider.calls] == ["submit", "get", "get"]
    value = journal.get("result")
    assert (
        journal.bytes("result", value["report_text_blob"]).decode()
        == "interim\n\n" + full_text + "\n\nhttps://example.org/book_(edition)."
    )
    assert value["material"]["blocks"][1]["annotations"] == [native]
    assert all(s["evidence_status"] == "reported_only" for s in value["material"]["reported_sources"])
    assert value["material"]["reported_sources"][-1]["url"] == "https://example.org/book_(edition)"
    export(journal, tmp_path / "export")
    assert (tmp_path / "export/report.md").read_bytes() == journal.bytes("result", value["report_text_blob"])
    assert (tmp_path / "export/exchange-3/response.bin").read_bytes() == completed.body
    export(journal, tmp_path / "export")
    other, _ = ready(monkeypatch)
    with pytest.raises(Conflict):
        export(other, tmp_path / "export")


def test_crash_after_receipt_recovers_without_network(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(
        response(
            "completed", steps=[dict(type="model_output", content=[dict(type="text", text="Full report")])]
        )
    )
    original = journal.put

    def crash(key, value):
        if key == "job":
            raise RuntimeError("power failure after durable receipt")
        return original(key, value)

    journal.put = crash
    with pytest.raises(RuntimeError):
        advance(journal, provider)
    journal.put = original
    assert advance(journal, provider)["state"] == "completed"
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "state", ["failed", "incomplete", "requires_action", "requires_continuation", "cancelled"]
)
def test_terminal_incomplete_outputs_not_accepted_as_report(monkeypatch, state):
    journal, _ = ready(monkeypatch)
    provider = Provider(response(state))
    assert advance(journal, provider)["state"] == state
    assert advance(journal, provider)["state"] == state
    assert not journal.get("result") and len(provider.calls) == 1


def test_cancel_uses_known_job_and_rejected_submit_does_not_retry(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(response(), response("cancelled"))
    advance(journal, provider)
    assert advance(journal, provider, operation="cancel")["state"] == "cancelled"
    assert [name for name, _ in provider.calls] == ["submit", "cancel"]
    journal, _ = ready(monkeypatch)
    provider = Provider(Response(429, {}, b"quota"))
    assert advance(journal, provider)["state"] == "provider_rejected"
    assert advance(journal, provider)["state"] == "provider_rejected"
    assert len(provider.calls) == 1


def test_missing_report_is_error_and_raw_receipt_survives(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(response("completed", steps=[]))
    with pytest.raises(ContractError, match="without_report"):
        advance(journal, provider)
    assert journal.get("exchange:1:receipt")
    assert status(journal)["state"] == "result_processing_required"
    with pytest.raises(ContractError):
        advance(journal, provider, operation="reprocess")
    assert len(provider.calls) == 1


def test_url_annotations_preserve_native_byte_offsets():
    native = dict(type="url_citation", url="https://example.org", start_index=0, end_index=12)
    material = report(
        dict(
            steps=[
                dict(type="model_output", content=[dict(type="text", text="Привет", annotations=[native])])
            ]
        )
    )
    assert material["blocks"][0]["report_end"] == 6
    assert material["blocks"][0]["annotations"][0]["end_index"] == 12


def test_interrupted_export_binding_can_be_retried(monkeypatch, tmp_path):
    import importlib

    module = importlib.import_module("podgen.research.hosted.export")
    journal, _ = ready(monkeypatch)
    original = module.os.link

    def interrupted(*args):
        raise OSError("interrupted before publication")

    monkeypatch.setattr(module.os, "link", interrupted)
    with pytest.raises(OSError):
        export(journal, tmp_path / "export")
    assert not list((tmp_path / "export").iterdir())
    monkeypatch.setattr(module.os, "link", original)
    export(journal, tmp_path / "export")
    assert json.loads((tmp_path / "export/binding.json").read_text())["run_id"] == str(journal.run_id)


def test_report_source_acquisition_is_resumable_and_not_evidence(monkeypatch):
    import asyncio

    from podgen.research.hosted.sources import SourceSelection, acquire_report_sources
    from podgen.sources.acquisition.contracts import AcquisitionResult

    journal, _ = ready(monkeypatch)
    provider = Provider(
        response(
            "completed",
            steps=[
                dict(
                    type="model_output",
                    content=[dict(type="text", text="https://example.org/one https://example.org/two")],
                )
            ],
        )
    )
    advance(journal, provider)
    ref = dict(kind="Policy", artifact_id=uuid4(), revision_id=uuid4())
    selection = SourceSelection(
        operation_id=uuid4(),
        profile_ref={**ref, "kind": "ProcessingProfile"},
        rights=dict(access_class="public_web", storage_policy_ref=ref, publication_policy_ref=ref),
    )
    calls = []

    async def acquire(repo, project, request):
        calls.append(request.url)
        if request.url.endswith("two") and len(calls) == 2:
            raise RuntimeError("process stopped")
        return AcquisitionResult(
            acquisition_record_ref=journal.ref("unused"),
            sources=[],
            status="failed",
            diagnostics=["unavailable fixture"],
        )

    with pytest.raises(RuntimeError):
        asyncio.run(acquire_report_sources(journal, selection, acquire=acquire))
    result = asyncio.run(acquire_report_sources(journal, selection, acquire=acquire))
    assert calls == ["https://example.org/one", "https://example.org/two", "https://example.org/two"]
    assert all(row["evidence_status"] == "no_exact_citation_binding" for row in result["sources"])
    assert all(row["acquisition"]["status"] == "failed" for row in result["sources"])
    assert asyncio.run(acquire_report_sources(journal, selection, acquire=acquire)) == result
    assert len(calls) == 3
    with pytest.raises(ContractError, match="not_in_report"):
        asyncio.run(
            acquire_report_sources(
                journal,
                selection.model_copy(update={"urls": ["https://example.org/invented"]}),
                acquire=acquire,
            )
        )
