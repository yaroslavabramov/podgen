import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from podgen.audio.fidelity.adjudication.realtime import URL
from podgen.audio.realtime_transport import RealtimeTransport
from podgen.runtime.evaluation.audio.adjudication.attempts import history, state
from podgen.runtime.evaluation.audio.adjudication.contracts import (
    ReviewConfig,
    ReviewConfigV2,
    ReviewConfigV3,
    parse_config,
)
from podgen.runtime.evaluation.audio.adjudication.export import export
from podgen.runtime.evaluation.audio.adjudication.local_resolution import key as local_key
from podgen.runtime.evaluation.audio.adjudication.prepare import prepare
from podgen.runtime.evaluation.audio.adjudication.providers import cost_for
from podgen.runtime.evaluation.audio.adjudication.service import ledger, reprocess, run
from podgen.runtime.evaluation.audio.adjudication.transport import ReviewerTransport
from tests.audio_adjudication_doubles import setup
from tests.audio_realtime_cases import Socket, configuration
from tests.test_audio_review_projection import wav


def routed(config, *, stall=None):
    google_calls, websocket_calls = [], []

    def google(request):
        google_calls.append(request)
        assert request.headers["x-goog-api-key"] == "private-review-key"
        assert "authorization" not in request.headers
        return httpx.Response(503, content=b"unavailable")

    client = httpx.Client(transport=httpx.MockTransport(google))
    transport = ReviewerTransport(config, client=client)

    async def connector(url, **kwargs):
        websocket_calls.append((url, kwargs))
        assert kwargs["additional_headers"] == {"Authorization": "Bearer private-openai-key"}
        return Socket(stall=stall)

    transport.routes[URL] = RealtimeTransport(timeout_seconds=0.03 if stall else 1, connector=connector)
    return transport, client, google_calls, websocket_calls


def test_version_three_native_provider_routing_frozen_projection_receipts_and_resume(monkeypatch, tmp_path):
    j, old = setup(monkeypatch)
    c = configuration(old)
    assert isinstance(parse_config(c.model_dump_json()), ReviewConfigV3)
    manifest = prepare(j, c)
    transport, client, primary, secondary = routed(c)
    try:
        run(j, c, transport)
        run(j, c, transport)
        reprocess(j, c, "Local event replay")
    finally:
        client.close()
    assert len(primary) == 1 and len(secondary) == 2
    assert ledger(j, manifest) == (3, Decimal("0.12004"))
    assert manifest["contract"] == "audio-review-manifest/3"
    for window in manifest["windows"]:
        value = j.get(window["key"])
        assert value["contract"] == "audio-review-window/3" and not j.get(local_key(window["key"]))
        row = history(j, window["key"])[-1]
        assert row["receipt"]["contract"] == "audio-review-websocket-receipt/1"
        assert row["receipt"]["handshake_status"] == 101 and row["receipt"]["response_done"]
        assert "status_code" not in row["receipt"] and state(j, window["key"]) == "reviewed"
    status = export(j, c, tmp_path)
    assert status["all_cases_resolved"] and status["requests_submitted"] == 3
    assert (tmp_path / "private/window-1/openai-transport.wav").exists()
    assert (tmp_path / "private/window-1/attempt-2/raw-websocket-events.json").exists()
    assert "private-openai-key" not in repr(j.records)


def test_unknown_websocket_outcome_never_reconnects_or_releases_reservation(monkeypatch):
    j, old = setup(monkeypatch)
    c = configuration(old)
    transport, client, primary, secondary = routed(c, stall="response.done")
    try:
        run(j, c, transport)
        run(j, c, transport)
    finally:
        client.close()
    manifest = prepare(j, c)
    assert len(primary) == len(secondary) == 1
    assert ledger(j, manifest) == (2, Decimal("0.30"))
    row = history(j, manifest["windows"][0]["key"])[-1]
    assert state(j, manifest["windows"][0]["key"]) == "outcome_unknown"
    assert row["receipt"]["handshake_status"] == 101 and not row["receipt"]["response_done"]
    assert not history(j, manifest["windows"][1]["key"])


def test_source_zero_is_inconclusive_with_no_api_or_reservation(monkeypatch, tmp_path):
    j, old = setup(monkeypatch)
    c = configuration(old)
    fid_key = "fidelity:" + str(c.fidelity_audit_id) + ":manifest"
    record = j.records[str(j.ref(fid_key).record_id)]
    record["audio_blob"] = j.blob(wav(seconds=5), "audio/wav")
    manifest = prepare(j, c)

    class NoNetwork:
        def submit(self, *args):
            pytest.fail("zero source must never reach network")

    monkeypatch.delenv("JEMINI_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY")
    run(j, c, NoNetwork())
    run(j, c, NoNetwork())
    reprocess(j, c, "No network")
    assert ledger(j, manifest) == (0, Decimal(0))
    for window in manifest["windows"]:
        local = j.get(local_key(window["key"]))
        assert local["basis"] == "all_source_pcm_samples_exactly_zero"
        assert all(
            r["status"] == "inconclusive" and r["reason_code"] == "audio_unavailable" and not r["heard_spans"]
            for r in local["output"]["results"]
        )
        assert not history(j, window["key"])
    status = export(j, c, tmp_path)
    assert status["counts"]["inconclusive"] == 2 and not status["production_ready"]
    assert status["status"] == "needs_attention" and status["requests_submitted"] == 0
    assert Decimal(status["required_reservation_usd"]) == 0


def test_legacy_schemas_and_requests_are_unchanged(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "docs/podcast-production/audio"
    for name, cls in [
        ("fidelity-adjudication.schema.json", ReviewConfig),
        ("fidelity-adjudication-v2.schema.json", ReviewConfigV2),
    ]:
        assert json.loads((root / name).read_text()) == cls.model_json_schema()
    j, c = setup(monkeypatch)
    manifest = prepare(j, c)
    assert manifest["contract"] == "audio-review-manifest/2"
    for window in manifest["windows"]:
        frozen = j.get(window["key"])
        assert "transport_audio_blob" not in frozen["providers"]["openai"]
        assert frozen["providers"]["openai"]["request"]["url"].endswith("chat/completions")


def test_realtime_price_counts_audio_and_reasoning_at_full_output_price(monkeypatch):
    _, old = setup(monkeypatch)
    c = configuration(old)
    path = Path(__file__).parent / "fixtures/audio/realtime-altered-response.json"
    usage = json.loads(path.read_text())["usage"]
    assert cost_for(c, "openai", usage)["usd"] == "0.013548"
    assert cost_for(c, "openai", dict(input_tokens=10))["usd"] == "0.20"
