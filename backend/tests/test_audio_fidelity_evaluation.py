import json
from copy import deepcopy
from uuid import uuid4

import pytest

from podgen.audio.fidelity.tokens import tokens
from podgen.audio.http import HttpReceipt, RawAudioTransport
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.audio.attempts import execute
from podgen.runtime.evaluation.audio.attempts import prefix as sample_prefix
from podgen.runtime.evaluation.audio.fidelity.contracts import FidelityConfig, prefix
from podgen.runtime.evaluation.audio.fidelity.export import export
from podgen.runtime.evaluation.audio.fidelity.prepare import prepare
from podgen.runtime.evaluation.audio.fidelity.processing import revisions
from podgen.runtime.evaluation.audio.fidelity.service import reprocess, run
from tests.test_audio_evaluation import Transport as SpeechTransport
from tests.test_audio_evaluation import fixture


def setup(monkeypatch):
    journal, comparison, manifest, cell = fixture(monkeypatch)
    execute(journal, manifest, cell, comparison, SpeechTransport())
    source = journal.records[str(cell["source_ref"]["record_id"])]
    source["source_script_ref"] = journal.ref("script").model_dump(mode="json")
    key = sample_prefix(cell["cell_id"], 1)
    config = FidelityConfig(
        audit_id=uuid4(),
        comparison_run_id=journal.run_id,
        result_ref=journal.ref(key + ":result"),
        reservation_usd="0.02",
        price=dict(
            usd_per_audio_minute="0.01",
            source_url="https://example.org/fixture-price",
            checked_at="2026-11-12",
        ),
    )
    monkeypatch.setenv("FISH_API_KEY", "fixture-secret")
    return journal, config


class ASR:
    def __init__(self, response=None, lost=False):
        self.calls, self.response, self.lost = [], response, lost

    def submit(self, request, body, credential):
        self.calls.append((request, body))
        if self.lost:
            raise RuntimeError("unknown delivery")
        if self.response:
            return self.response
        text = "Точное русское произношение. Лишние слова."
        return HttpReceipt(
            200,
            {"content-type": "application/json"},
            json.dumps(
                dict(
                    text=text,
                    duration=5,
                    segments=[
                        dict(text=t["text"], start=i * 0.5, end=i * 0.5 + 0.4)
                        for i, t in enumerate(tokens(text))
                    ],
                    request_id="fixture-request",
                )
            ).encode(),
            20,
        )


def test_success_resume_and_reprocess_archive_exact_bytes_without_second_payment(monkeypatch, tmp_path):
    j, c = setup(monkeypatch)
    transport = ASR()
    assert run(j, c, transport) == "candidates_generated"
    frozen = deepcopy(j.get(prefix(c.audit_id) + ":manifest"))
    assert frozen["audio_variant"] == "decoded_pcm_before_playback_gain"
    assert frozen["playback_audio_blob"]
    assert frozen["transport_proof"]["pcm_bit_identical"] and not frozen["expected_text_sent_to_recognizer"]
    assert transport.calls[0][1] == j.bytes(prefix(c.audit_id) + ":manifest", frozen["wire_blob"])
    monkeypatch.delenv("FISH_API_KEY")
    assert run(j, c, transport) == "candidates_generated" and len(transport.calls) == 1
    assert reprocess(j, c, "Recompute candidate stage from saved receipt") == "candidates_generated"
    assert len(revisions(j, c.audit_id)) == 2 and j.get(prefix(c.audit_id) + ":manifest") == frozen
    status = export(j, c, tmp_path)
    assert status["requests_submitted"] == 1 and status["candidates"] > 0 and not status["production_ready"]
    receipt = json.loads((tmp_path / "private/receipt.json").read_text())
    assert (
        j.bytes(prefix(c.audit_id) + ":receipt", receipt["raw_response_blob"])
        == (tmp_path / "private/raw-http-response.bin").read_bytes()
    )
    assert list((tmp_path / "private/processing-1").glob("*.wav"))
    assert "fixture-secret" not in repr(j.records)
    with pytest.raises(Conflict):
        prepare(j, c.model_copy(update=dict(clip_margin_seconds=2)))
    second = c.model_copy(update=dict(audit_id=uuid4()))
    with pytest.raises(Conflict, match="already_requested"):
        monkeypatch.setenv("FISH_API_KEY", "fixture-secret")
        run(j, second, transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,response",
    [
        ("provider_rejected", HttpReceipt(402, {}, b'{"message":"credit"}', 5)),
        ("outcome_unknown", HttpReceipt(200, {}, b"partial", 5, False, "ReadTimeout")),
        ("response_invalid", HttpReceipt(200, {}, b'{"malformed":true}', 5)),
    ],
)
def test_failed_or_unknown_asr_never_resubmits_and_keeps_raw_receipt(monkeypatch, status, response):
    j, c = setup(monkeypatch)
    transport = ASR(response)
    assert run(j, c, transport) == status
    assert run(j, c, transport) == status and len(transport.calls) == 1
    receipt = j.get(prefix(c.audit_id) + ":receipt")
    assert j.bytes(prefix(c.audit_id) + ":receipt", receipt["raw_response_blob"]) == response.body
    if status != "response_invalid":
        with pytest.raises(ContractError):
            reprocess(j, c, "Do not parse unsuccessful receipt")


def test_lost_ack_and_missing_key_are_distinct_and_budget_checked_before_submit(monkeypatch):
    j, c = setup(monkeypatch)
    monkeypatch.delenv("FISH_API_KEY")
    transport = ASR(lost=True)
    assert run(j, c, transport) == "credential_missing" and not transport.calls
    monkeypatch.setenv("FISH_API_KEY", "fixture-secret")
    with pytest.raises(RuntimeError):
        run(j, c, transport)
    assert run(j, c, transport) == "outcome_unknown" and len(transport.calls) == 1
    assert j.get(prefix(c.audit_id) + ":submit")["reservation_usd"] == "0.02"
    j2, c2 = setup(monkeypatch)
    with pytest.raises(ContractError, match="reservation_below"):
        prepare(j2, c2.model_copy(update=dict(reservation_usd="0.00001")))


def test_raw_audio_http_transport_keeps_partial_bytes_and_sends_no_retry():
    import httpx

    calls = []

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("connection lost")

    def handler(req):
        calls.append(req)
        assert req.headers["authorization"] == "Bearer secret"
        assert req.headers["model"] == "transcribe-1-pro" and req.content == b"exact wire"
        return httpx.Response(200, stream=Broken())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = RawAudioTransport(client=client).submit(
            dict(url="https://api.fish.audio/v1/asr", headers={"model": "transcribe-1-pro"}),
            b"exact wire",
            "secret",
        )
    assert len(calls) == 1 and result.body == b"partial" and not result.complete
