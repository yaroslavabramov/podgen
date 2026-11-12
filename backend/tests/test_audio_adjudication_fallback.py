import json
from decimal import Decimal

import httpx
import pytest

from podgen.audio.http import HttpReceipt
from podgen.runtime.evaluation.audio.adjudication.attempts import history, state
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfig, ReviewConfigV2, parse_config
from podgen.runtime.evaluation.audio.adjudication.export import export
from podgen.runtime.evaluation.audio.adjudication.fallback import switch_key
from podgen.runtime.evaluation.audio.adjudication.prepare import prepare
from podgen.runtime.evaluation.audio.adjudication.processing import process
from podgen.runtime.evaluation.audio.adjudication.providers import cost_for
from podgen.runtime.evaluation.audio.adjudication.service import ledger, reprocess, run
from podgen.runtime.evaluation.audio.adjudication.transport import ReviewerTransport
from tests.audio_adjudication_doubles import Routed, configuration, openai_response, setup
from tests.test_audio_adjudication_runtime import Reviewer, review_fixture


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504, 520, 599])
def test_transient_failure_switches_once_for_entire_run(monkeypatch, tmp_path, code):
    j, c = setup(monkeypatch)
    manifest = prepare(j, c)
    t = Routed(HttpReceipt(code, {}, b"refused", 1))
    run(j, c, t)
    assert len(t.calls) == 3
    assert [call[2] for call in t.calls] == ["private-review-key", "private-openai-key", "private-openai-key"]
    first, second = manifest["windows"]
    assert len(history(j, first["key"])) == 2 and len(history(j, second["key"])) == 1
    assert all(state(j, w["key"]) == "reviewed" for w in manifest["windows"])
    assert ledger(j, manifest) == (3, Decimal("0.1143750"))
    run(j, c, t)
    reprocess(j, c, "Local replay")
    assert len(t.calls) == 3
    status = export(j, c, tmp_path)
    assert status["active_provider"] == "openai" and status["all_cases_resolved"]
    assert (tmp_path / "private/window-1/openai-wire-request.json").read_bytes() == t.calls[1][1]
    assert "private-openai-key" not in repr(j.records)


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_nonavailability_failure_does_not_switch(monkeypatch, code):
    j, c = setup(monkeypatch)
    t = Routed(HttpReceipt(code, {}, b"refused", 1))
    run(j, c, t)
    run(j, c, t)
    assert len(t.calls) == 1 and j.get(switch_key(c)) is None


def test_invalid_model_answer_does_not_switch(monkeypatch):
    j, c = setup(monkeypatch)
    t = Routed(HttpReceipt(200, {}, b"invalid json", 1))
    run(j, c, t)
    assert len(t.calls) == 1 and j.get(switch_key(c)) is None
    assert state(j, prepare(j, c)["windows"][0]["key"]) == "response_invalid"


def test_inconclusive_answer_does_not_trigger_more_requests(monkeypatch):
    j, c = setup(monkeypatch)
    t = Routed(secondary_status="inconclusive")
    run(j, c, t)
    run(j, c, t)
    assert len(t.calls) == 3


@pytest.mark.parametrize(
    "status,reason", [("confirmed", "audible_mismatch"), ("inconclusive", "unclear_speech")]
)
def test_primary_acoustic_verdict_never_switches_provider(monkeypatch, status, reason):
    from tests.test_audio_repair import ConfirmedReviewer

    j, c = setup(monkeypatch)
    t = ConfirmedReviewer(status, reason)
    run(j, c, t)
    manifest = prepare(j, c)
    assert j.get(switch_key(c)) is None
    assert all(len(history(j, w["key"])) == 1 for w in manifest["windows"])
    assert all(state(j, w["key"]) == "reviewed" for w in manifest["windows"])


def test_repair_consumes_version_two_config_and_rejects_superseded_primary(monkeypatch):
    from uuid import uuid4

    from podgen.domain.errors import ContractError
    from podgen.runtime.evaluation.audio.adjudication.contracts import validate_config
    from podgen.runtime.evaluation.audio.adjudication.fallback import switch
    from podgen.runtime.evaluation.audio.repair.prepare import prepare as prepare_repair
    from tests.test_audio_repair import ConfirmedReviewer, fixture

    j, repair = fixture(monkeypatch)
    original = j.get("audio-review:" + str(repair.review_id) + ":manifest")
    review = configuration(
        validate_config(original["configuration"]).model_copy(update=dict(review_id=uuid4()))
    )
    run(j, review, ConfirmedReviewer())
    window = prepare(j, review)["windows"][0]
    row = history(j, window["key"])[-1]
    repaired = repair.model_copy(
        update=dict(
            review_id=review.review_id, review_result_ref=j.ref(row["processing"][-1]["key"] + ":result")
        )
    )
    assert prepare_repair(j, repaired)
    switch(j, review, row, "outcome_unknown")
    with pytest.raises(ContractError, match="latest_validated"):
        prepare_repair(j, repaired.model_copy(update=dict(repair_id=uuid4())))


@pytest.mark.parametrize(
    "receipt",
    [
        HttpReceipt(0, {}, b"", 1, False, "ReadTimeout"),
        HttpReceipt(200, {}, b"partial", 1, False, "ReadError"),
    ],
)
def test_unknown_receipt_retains_reservation(monkeypatch, receipt):
    j, c = setup(monkeypatch)
    t = Routed(receipt)
    run(j, c, t)
    manifest = prepare(j, c)
    assert ledger(j, manifest) == (3, Decimal("0.1143750"))
    first = history(j, manifest["windows"][0]["key"])
    assert first[0]["receipt"]["complete"] is False
    assert first[1]["prepared"]["parent_attempt_ref"] == j.ref(first[0]["key"] + ":prepared").model_dump(
        mode="json"
    )


def test_unknown_intent_resume_and_late_primary_cannot_replace_fallback(monkeypatch, tmp_path):
    j, c = setup(monkeypatch)
    t = Reviewer(lost=True)
    with pytest.raises(RuntimeError):
        run(j, c, t)
    good = Routed()
    run(j, c, good)
    assert len(t.calls) == 1 and len(good.calls) == 2
    manifest = prepare(j, c)
    primary = history(j, manifest["windows"][0]["key"])[0]
    # A late primary reply is retained but never becomes the accepted decision.
    response = Reviewer().submit({}, t.calls[0], "key")
    j.put(
        primary["key"] + ":receipt",
        dict(
            status_code=200,
            headers={},
            complete=True,
            error=None,
            raw_response_blob=j.blob(response.body, "application/json"),
        ),
    )
    process(j, primary["key"], c)
    reprocess(j, c, "Ignore superseded primary")
    run(j, c, good)
    assert len(good.calls) == 2 and ledger(j, manifest) == (3, Decimal("0.1143750"))
    export(j, c, tmp_path)
    report = json.loads((tmp_path / "decisions.json").read_text())
    assert all("Mechanical" in d["explanation"] for d in report["decisions"])


@pytest.mark.parametrize("limits", [dict(max_requests=1), dict(max_cost_usd="0.25")])
def test_global_budget_prevents_fallback_dispatch(monkeypatch, tmp_path, limits):
    j, c = setup(monkeypatch, **limits)
    t = Routed()
    run(j, c, t)
    run(j, c, t)
    assert len(t.calls) == 1
    result = export(j, c, tmp_path)
    assert result["requests_submitted"] == 1
    assert (
        json.loads((tmp_path / "decisions.json").read_text())["windows"][0]["blocked_reason"]
        == "budget_exhausted"
    )


def test_missing_fallback_key_resumes_without_resubmitting_primary(monkeypatch):
    j, c = setup(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY")
    t = Routed()
    run(j, c, t)
    assert len(t.calls) == 1
    monkeypatch.setenv("OPENAI_API_KEY", "restored")
    run(j, c, t)
    assert len(t.calls) == 3 and t.calls[1][2] == "restored"


def test_version_one_roundtrip_shape_unchanged(monkeypatch):
    _, c = review_fixture(monkeypatch)
    raw = c.model_dump(mode="json")
    parsed = parse_config(c.model_dump_json())
    assert type(parsed) is ReviewConfig and parsed.model_dump(mode="json") == raw
    assert "fallback" not in raw
    assert isinstance(parse_config(configuration(c).model_dump_json()), ReviewConfigV2)


def test_provider_transport_routes_actual_auth_headers_and_exact_wire(monkeypatch):
    j, c = setup(monkeypatch)
    manifest = prepare(j, c)
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == "private-review-key"
            assert "authorization" not in request.headers
            return httpx.Response(503, content=b"unavailable")
        assert request.headers["authorization"] == "Bearer private-openai-key"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200, content=openai_response(request.content))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = ReviewerTransport(c, client=client)
        assert (
            transport.routes["https://api.openai.com/v1/chat/completions"].timeout_seconds
            == c.fallback.reviewer.request_timeout_seconds
        )
        run(j, c, transport)
    assert len(requests) == 3
    for request, provider, window in zip(
        requests, ["gemini", "openai", "openai"], [manifest["windows"][0], *manifest["windows"]], strict=True
    ):
        frozen = j.get(window["key"])["providers"][provider]
        assert request.content == j.bytes(window["key"], frozen["wire_blob"])


def test_missing_modality_usage_keeps_reservation(monkeypatch):
    _, c = setup(monkeypatch)
    assert cost_for(c, "openai", dict(prompt_tokens=100, completion_tokens=10))["usd"] == "0.20"


@pytest.mark.parametrize(
    "secondary", [HttpReceipt(503, {}, b"unavailable", 1), HttpReceipt(0, {}, b"", 1, False, "ReadTimeout")]
)
def test_fallback_failure_stops_without_automatic_retry_or_switchback(monkeypatch, secondary):
    j, c = setup(monkeypatch)
    calls = []

    class FailedProviders:
        def submit(self, request, wire, credential):
            calls.append(request["url"])
            return secondary if "openai.com" in request["url"] else HttpReceipt(503, {}, b"primary", 1)

    t = FailedProviders()
    run(j, c, t)
    run(j, c, t)
    manifest = prepare(j, c)
    assert len(calls) == 2 and ledger(j, manifest) == (2, Decimal("0.30"))
    assert not history(j, manifest["windows"][1]["key"])
