import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from podgen import toneperfect_qualification_cli as cli
from podgen.audio.http import HttpReceipt, RawAudioTransport
from podgen.audio.toneperfect import URL
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.toneperfect.contracts import Trial
from podgen.runtime.evaluation.toneperfect.export import export
from podgen.runtime.evaluation.toneperfect.prepare import prepare
from podgen.runtime.evaluation.toneperfect.service import run
from podgen.runtime.evaluation.toneperfect.store import Store
from tests.audio_cases import signal


def fixture(tmp_path, monkeypatch, seconds=1):
    audio = signal(seconds=seconds)
    path = tmp_path / "source.wav"
    path.write_bytes(audio)
    config = Trial(
        run_id=uuid4(),
        cases=[
            dict(
                case_id=f"C{i}",
                audio_path=path,
                audio_sha256=hashlib.sha256(audio).hexdigest(),
                reference_text=f"Полная фраза номер {i}.",
                provenance=dict(source="frozen-fixture"),
                evaluation=dict(expected="private-gold", asr="never sent"),
            )
            for i in range(8)
        ],
    )
    monkeypatch.setenv("TONEPERFECT_API_KEY", "tp_live_fixture_secret")
    return config, Store(tmp_path / "trial")


def reply(**changes):
    value = dict(
        id="request-fixture",
        assessable=True,
        status="completed",
        words=[dict(word="Фраза", score=80)],
        scores=dict(overall=80),
        model_version="ru-fixture-v1",
        model=dict(notes="all diagnostics"),
        engine_route=dict(engine="primary", fallback=False, reason=None),
        billing=dict(credits=1, source="free", free_credits=40),
        raw=dict(trace=[1, 2, 3]),
    )
    value.update(changes)
    return HttpReceipt(200, {}, json.dumps(value, ensure_ascii=False).encode(), 1)


class Transport:
    def __init__(self, response=None, lose=False):
        self.calls, self.response, self.lose = [], response, lose

    def submit(self, request, wire, credential):
        self.calls.append((deepcopy(request), wire, credential))
        if self.lose:
            raise RuntimeError("lost acknowledgement")
        return self.response or reply()


def test_prepare_freezes_whole_audio_reference_and_only_provider_fields(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    manifest = prepare(store, config)
    assert manifest["reservation_credits"] == 8 and len(manifest["cases"]) == 8
    assert len({c["idempotency_key"] for c in manifest["cases"]}) == 8
    for case, original in zip(manifest["cases"], config.cases, strict=True):
        request, wire = case["request"], store.bytes(case["wire_path"])
        assert request["url"] == URL
        assert request["fields"] == dict(text=original.reference_text, language="ru-RU", task="general")
        assert request["headers"]["TonePerfect-Fallback"] == "off"
        assert request["headers"]["TonePerfect-Debug"] == "raw"
        assert original.audio_path.read_bytes() in wire
        assert b"private-gold" not in wire and b"never sent" not in wire
        assert b"tp_live_fixture_secret" not in wire and "authorization" not in request["headers"]
    old = deepcopy(manifest)
    config.cases[0].audio_path.write_bytes(b"changed source")
    assert prepare(store, config) == old
    with pytest.raises(Conflict):
        prepare(store, config.model_copy(update=dict(max_credits=9)))


def test_eight_sequential_real_auth_mocked_http_and_resume_no_duplicates(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer tp_live_fixture_secret"
        assert request.headers["idempotency-key"].startswith("podgen-toneperfect-")
        assert request.headers["toneperfect-fallback"] == "off"
        assert b'name="text"' in request.content and b'name="audio"' in request.content
        response = reply()
        return httpx.Response(200, content=response.body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = RawAudioTransport(client=client)
        run(store, config, transport)
        run(store, config, transport)
    report = export(store, config)
    assert len(requests) == 8 and report["credits_or_reservation"] == 8
    assert report["status"] == "responses_saved" and not report["production_ready"]
    assert report["qualification_status"] == "not_evaluated"
    assert all(c["provider_response"]["raw"] == dict(trace=[1, 2, 3]) for c in report["cases"])


def test_two_case_followup_submits_only_requested_cases(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    data = config.model_dump()
    data["cases"] = data["cases"][:2]
    data["max_credits"] = 2
    config = Trial.model_validate(data)
    transport = Transport()
    run(store, config, transport)
    run(store, config, transport)
    report = export(store, config)
    assert len(transport.calls) == 2
    assert report["status"] == "responses_saved"
    assert report["credits_or_reservation"] == 2
    for cases in ([], data["cases"] * 5):
        with pytest.raises(ValueError):
            Trial.model_validate(dict(data, cases=cases))


@pytest.mark.parametrize(
    "response",
    [
        HttpReceipt(504, {}, b"<html>gateway</html>", 1),
        HttpReceipt(0, {}, b"partial", 1, False, "ReadTimeout"),
        HttpReceipt(409, {}, b'{"error":{"code":"request_pending"}}', 1),
        HttpReceipt(503, {}, b'{"error":{"code":"engine_unavailable"}}', 1),
    ],
)
def test_explicit_retry_uses_same_key_and_exact_body_without_double_booking(tmp_path, monkeypatch, response):
    config, store = fixture(tmp_path, monkeypatch)
    first = Transport(response)
    run(store, config, first)
    run(store, config, first)
    assert len(first.calls) == 1 and export(store, config)["credits_or_reservation"] == 1
    second = Transport()
    run(store, config, second, retry_case="C0", reason="Operator fetches same pending assessment")
    assert first.calls[0] == second.calls[0]
    assert export(store, config)["credits_or_reservation"] == 1
    assert export(store, config)["requests_submitted"] == 2
    with pytest.raises(ContractError):
        run(store, config, second, retry_case="C0", reason="No repeated assessment")


def test_unknown_intent_survives_process_loss_and_uses_same_key(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    lost = Transport(lose=True)
    with pytest.raises(RuntimeError):
        run(store, config, lost)
    run(store, config, lost)
    assert len(lost.calls) == 1
    good = Transport()
    run(store, config, good, retry_case="C0", reason="Recover lost receipt safely")
    assert lost.calls[0] == good.calls[0]


@pytest.mark.parametrize("code", [401, 402, 422, 429])
def test_provider_failure_stops_and_preserves_raw_body(tmp_path, monkeypatch, code):
    config, store = fixture(tmp_path, monkeypatch)
    response = HttpReceipt(code, {}, b'{"error":{"code":"failure"}}', 1)
    transport = Transport(response)
    run(store, config, transport)
    run(store, config, transport)
    assert len(transport.calls) == 1
    assert store.bytes("cases/C0/attempt-1/response.bin") == response.body


@pytest.mark.parametrize(
    "changes",
    [
        dict(billing=dict(credits=1, source="plan")),
        dict(billing=dict(credits=1)),
        dict(engine_route=dict(engine="fallback", fallback=True)),
        dict(test=True),
    ],
)
def test_policy_violation_stops_before_next_case_and_on_resume(tmp_path, monkeypatch, changes):
    config, store = fixture(tmp_path, monkeypatch)
    transport = Transport(reply(**changes))
    run(store, config, transport)
    run(store, config, transport)
    assert len(transport.calls) == 1
    assert export(store, config)["cases"][0]["policy_violations"]


def test_free_balance_prevents_next_paid_request(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    transport = Transport(reply(billing=dict(credits=1, source="free", free_credits=0)))
    with pytest.raises(ContractError, match="free_balance"):
        run(store, config, transport)
    assert len(transport.calls) == 1


def test_model_change_is_retained_and_blocks_further_calls(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    first = Transport()
    run(store, config, first, case_id="C0")
    changed = Transport(reply(model_version="ru-v2"))
    run(store, config, changed, case_id="C1")
    with pytest.raises(ContractError, match="previous_policy"):
        run(store, config, changed, case_id="C2")
    assert len(changed.calls) == 1 and export(store, config)["cases"][1]["model_changed"]


def test_raw_saved_before_parse_and_receipt_can_be_processed_without_network(tmp_path, monkeypatch):
    from podgen.runtime.evaluation.toneperfect import observations

    config, store = fixture(tmp_path, monkeypatch)
    original = observations.parse

    def crash(*args, **kwargs):
        assert store.bytes("cases/C0/attempt-1/response.bin") == reply().body
        raise RuntimeError("local parser lost process")

    monkeypatch.setattr(observations, "parse", crash)
    transport = Transport()
    with pytest.raises(RuntimeError):
        run(store, config, transport)
    monkeypatch.setattr(observations, "parse", original)
    run(store, config, transport, case_id="C0")
    assert len(transport.calls) == 1 and store.get("cases/C0/attempt-1/result.json")


def test_credit_preflight_and_wire_corruption_block_before_network(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch, seconds=16)
    with pytest.raises(ContractError, match="exceeds_credit_cap"):
        prepare(store, config)
    config, _ = fixture(tmp_path, monkeypatch)
    store = Store(tmp_path / "fresh")
    manifest = prepare(store, config)
    (store.root / manifest["cases"][0]["wire_path"]).write_bytes(b"corrupted")
    transport = Transport()
    with pytest.raises(ContractError, match="wire_hash"):
        run(store, config, transport)
    assert not transport.calls


def test_mock_key_and_foreign_directory_rejected_without_key_output(tmp_path, monkeypatch, capsys):
    config, store = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("TONEPERFECT_API_KEY", "tp_test_not-real")
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    assert cli.main(["run", "--config", str(path), "--output-dir", str(store.root)]) == 2
    assert "tp_test_not-real" not in capsys.readouterr().out
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes").write_text("leave intact")
    with pytest.raises(Conflict):
        Store(foreign)


@pytest.mark.parametrize("code", ["idempotency_conflict", "idempotency_expired"])
def test_terminal_idempotency_errors_never_generate_new_key(tmp_path, monkeypatch, code):
    config, store = fixture(tmp_path, monkeypatch)
    transport = Transport(HttpReceipt(409, {}, json.dumps(dict(error=dict(code=code))).encode(), 1))
    run(store, config, transport)
    with pytest.raises(ContractError, match="not_retryable"):
        run(store, config, transport, retry_case="C0", reason="Explicit check")
    assert len(transport.calls) == 1


def test_unassessable_zero_charge_and_large_raw_are_preserved(tmp_path, monkeypatch):
    config, store = fixture(tmp_path, monkeypatch)
    large = "full diagnostics " * 10000
    response = reply(
        assessable=False,
        status="unassessable",
        reason="no_speech",
        words=[],
        scores=None,
        billing=dict(credits=0),
        raw=dict(diagnostic=large),
    )
    transport = Transport(response)
    run(store, config, transport, case_id="C0")
    report = export(store, config)
    assert store.bytes("cases/C0/attempt-1/response.bin") == response.body
    assert report["cases"][0]["provider_response"]["raw"]["diagnostic"] == large
    assert report["credits_or_reservation"] == 0
