"""Offline checks for equal conditions, durable spend guards and exact blind replay."""

import copy
import json
import shutil
from pathlib import Path

import pytest
from artifacts import read, save, send_once
from conditions import TASK, author_request, payload, words
from providers import probe, select
from results import estimate, replay

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "evals/reports/2026-11-08-author-openings"


def envelope(provider, model, output):
    text = json.dumps(output, ensure_ascii=False)
    if provider == "anthropic":
        return dict(
            id="msg_test",
            model=model,
            stop_reason="end_turn",
            content=[dict(type="text", text=text)],
            usage=dict(input_tokens=20, output_tokens=10),
        )
    return dict(
        id="resp_test",
        model=model,
        status="completed",
        output=[dict(type="message", content=[dict(type="output_text", text=text)])],
        usage=dict(input_tokens=20, output_tokens=10, input_tokens_details={"cached_tokens": 0}),
    )


def fake_transport(response, counter):
    def send(body, client_id):
        counter.append(client_id)
        return 200, "request-test", json.dumps(response, ensure_ascii=False).encode()

    return send


def test_prepared_conditions_preserve_the_complete_original_and_plan():
    original = read(REPORT / "source/request.json")
    common = read(REPORT / "common/payload.json")
    expected = payload(original)
    assert common.pop("opening_task") == TASK
    assert common == expected
    assert (REPORT / "common/instructions.txt").read_text() == original["instructions"]
    assert read(REPORT / "common/schema.json") == original["text"]["format"]["schema"]
    assert read(REPORT / "source/accepted-plan.json")["output"]["status"] == "ready"
    excerpt = (REPORT / "historical/opening.md").read_text().removesuffix("\n")
    full = "\n\n".join(row["text"] for row in read(REPORT / "historical/full-script.json")["utterances"])
    assert full.startswith(excerpt)
    assert 220 <= words(excerpt) <= 270


def test_both_wire_adapters_send_identical_full_author_content():
    original, common = read(REPORT / "source/request.json"), read(REPORT / "common/payload.json")
    a = author_request(original, common, "openai", "gpt-6.1-sol")
    b = author_request(original, common, "anthropic", "claude-opus-5-5")
    assert a["instructions"] == b["system"] == original["instructions"]
    assert payload(a) == json.loads(b["messages"][0]["content"][0]["text"]) == common
    assert a["reasoning"]["effort"] == b["output_config"]["effort"] == "medium"
    assert a["text"] == original["text"]
    assert (
        "minLength"
        not in b["output_config"]["format"]["schema"]["$defs"]["WriteUtterance"]["properties"]["text"]
    )
    assert (
        original["text"]["format"]["schema"]["$defs"]["WriteUtterance"]["properties"]["text"]["minLength"]
        == 1
    )


def test_resuming_a_saved_response_never_spends_twice_and_changed_request_is_rejected(tmp_path):
    calls = []
    transport = fake_transport({"id": "response"}, calls)
    assert send_once(tmp_path, {"model": "test"}, transport) == {"id": "response"}
    assert send_once(tmp_path, {"model": "test"}, transport) == {"id": "response"}
    assert len(calls) == 1
    with pytest.raises(ValueError, match="immutable_artifact_mismatch"):
        send_once(tmp_path, {"model": "changed"}, transport)
    assert len(calls) == 1


def test_ambiguous_network_failure_cannot_be_retried(tmp_path):
    calls = []

    def timeout(body, client_id):
        calls.append(client_id)
        raise TimeoutError("Outcome unknown")

    with pytest.raises(TimeoutError):
        send_once(tmp_path, {"model": "test"}, timeout)
    with pytest.raises(RuntimeError, match="reconcile"):
        send_once(tmp_path, {"model": "test"}, timeout)
    assert len(calls) == 1


def test_listing_alone_does_not_prove_generation_access(tmp_path):
    response = dict(error={"type": "permission_error"})

    def denied(body, client_id):
        return 403, "denied", json.dumps(response).encode()

    assert not probe(tmp_path, "anthropic", "listed-only", denied)
    assert read(tmp_path / "transport.json")["status"] == 403


def test_replay_is_offline_and_does_not_edit_or_filter_nonpreferred_length(tmp_path):
    original, common = read(REPORT / "source/request.json"), read(REPORT / "common/payload.json")
    for name in ("source", "conditions.json"):
        source = REPORT / name
        if source.is_dir():
            shutil.copytree(source, tmp_path / name)
        else:
            shutil.copy2(source, tmp_path / name)
    save(tmp_path / "common/payload.json", common)
    (tmp_path / "common/instructions.txt").write_text(original["instructions"])
    save(tmp_path / "pricing.json", read(REPORT / "pricing.json"))
    identity = {
        "A": dict(slot="one", provider="openai", model="gpt-6.1-sol"),
        "B": dict(slot="two", provider="anthropic", model="claude-opus-5-5"),
    }
    save(tmp_path / "sealed/identity.json", identity)
    calls = []
    for label, entry in identity.items():
        output = dict(utterances=[dict(speaker="РАССКАЗЧИК", text="Точный короткий текст " + label + ".")])
        request = author_request(original, common, entry["provider"], entry["model"])
        send_once(
            tmp_path / "sealed/candidates" / entry["slot"],
            request,
            fake_transport(envelope(entry["provider"], entry["model"], output), calls),
        )
    first = replay(tmp_path)
    assert replay(tmp_path) == first
    assert len(calls) == 2
    assert (tmp_path / "blind/A.md").read_text() == "Точный короткий текст A.\n"
    assert not read(tmp_path / "blind/checks.json")["samples"][0]["within_guidance"]
    changed = copy.deepcopy(common)
    changed["story"]["main_question"] = "tampered"
    path = tmp_path / "sealed/candidates/one/request.json"
    value = read(path)
    value["input"][0]["content"][0]["text"] = json.dumps(changed)
    path.write_text(json.dumps(value))
    with pytest.raises(AssertionError):
        replay(tmp_path)


def test_cost_includes_cache_categories_and_unknown_models_are_not_guessed():
    response = dict(
        model="known",
        usage=dict(
            input_tokens=100,
            output_tokens=20,
            input_tokens_details=dict(cached_tokens=30, cache_write_tokens=10),
        ),
    )
    rates = {"known": {"input": "2", "output": "10", "cached": ".1", "write": "2.5"}}
    assert estimate(response, "openai", rates) == "0.000348"
    assert estimate(dict(response, model="unknown"), "openai", rates) is None


def test_bad_probe_parameters_do_not_authorize_model_fallback(tmp_path):
    def unsupported(body, client_id):
        return 400, "unsupported", json.dumps({"error": {"param": "reasoning.effort"}}).encode()

    with pytest.raises(RuntimeError, match="contract rejected"):
        probe(tmp_path, "openai", "test-model", unsupported)


def test_fallback_uses_newest_listed_active_opus_then_sonnet(tmp_path):
    called = []

    class Fake:
        def __init__(self, provider):
            self.provider = provider

        def models(self):
            rows = (
                [{"id": "gpt-6.1-sol"}]
                if self.provider == "openai"
                else [
                    {"id": "older-opus", "line": "opus", "lifecycle": "active", "created_at": "2026-01-01"},
                    {"id": "latest-opus", "line": "opus", "lifecycle": "active", "created_at": "2026-09-01"},
                    {
                        "id": "latest-sonnet",
                        "line": "sonnet",
                        "lifecycle": "active",
                        "created_at": "2026-10-01",
                    },
                ]
            )
            return {"data": rows, "http_status": 200}

        def __call__(self, body, client_id):
            model = body["model"]
            called.append(model)
            if model == "latest-opus":
                return 403, "denied", json.dumps({"error": {"type": "permission_error"}}).encode()
            value = envelope(self.provider, model, {})
            return 200, "accepted", json.dumps(value).encode()

    result = select(tmp_path, {name: Fake(name) for name in ("openai", "anthropic")})
    assert called == ["gpt-6.1-sol", "latest-opus", "latest-sonnet"]
    assert result[-1] == {"provider": "anthropic", "model": "latest-sonnet"}
    assert select(tmp_path, {}) == result
    assert len(called) == 3
