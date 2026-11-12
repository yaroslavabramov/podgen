import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.audio.fidelity.tokens import tokens
from podgen.audio.http import HttpReceipt
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.adjudication.attempts import history, state
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfig
from podgen.runtime.evaluation.audio.adjudication.export import export
from podgen.runtime.evaluation.audio.adjudication.prepare import prepare
from podgen.runtime.evaluation.audio.adjudication.service import ledger, reprocess, run
from podgen.runtime.evaluation.audio.fidelity.processing import revisions
from podgen.runtime.evaluation.audio.fidelity.service import run as recognize
from tests.test_audio_fidelity_evaluation import ASR, setup


def review_fixture(monkeypatch, **overrides):
    journal, asr = setup(monkeypatch)
    text = "Странное русское произношение. Без лишних звуков."
    response = dict(
        text=text,
        duration=5,
        segments=[dict(text=t["text"], start=i * 0.5, end=i * 0.5 + 0.4) for i, t in enumerate(tokens(text))],
        request_id="fixture",
    )
    assert (
        recognize(journal, asr, ASR(HttpReceipt(200, {}, json.dumps(response).encode(), 5)))
        == "candidates_generated"
    )
    prompt = Path(__file__).resolve().parents[2] / "evals/audio/listening/local-fidelity-review.prompt.md"
    config = ReviewConfig(
        review_id=uuid4(),
        comparison_run_id=journal.run_id,
        fidelity_audit_id=asr.audit_id,
        candidate_result_ref=journal.ref(revisions(journal, asr.audit_id)[-1]["key"] + ":result"),
        prompt_path=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        price=dict(
            usd_per_million_input_tokens="1",
            usd_per_million_output_tokens="2",
            source_url="https://example.org/fixture",
            checked_at="2026-11-12",
        ),
        reservation_per_request_usd="0.10",
        max_cost_usd="1",
        max_cases_per_window=1,
        **overrides,
    )
    monkeypatch.setenv("JEMINI_API_KEY", "private-review-key")
    return journal, config


class Reviewer:
    def __init__(self, response=None, lost=False):
        self.calls = []
        self.response = response
        self.lost = lost

    def submit(self, request, wire, credential):
        self.calls.append(wire)
        if self.lost:
            raise RuntimeError("lost acknowledgement")
        if self.response is not None:
            return self.response
        payload = json.loads(json.loads(wire)["input"][0]["text"])
        results = [
            dict(
                case_id=c["case_id"],
                status="dismissed",
                reason_code="asr_error",
                heard_spans=[
                    dict(
                        start_seconds=0,
                        end_seconds=payload["audio_window"]["duration_seconds"] / 2,
                        words="слышимые слова",
                    )
                ],
                explanation="Контрактная fixture, не оценка голоса.",
            )
            for c in payload["cases"]
        ]
        return HttpReceipt(
            200,
            {},
            json.dumps(
                dict(
                    status="completed",
                    id="fixture",
                    steps=[
                        dict(
                            type="model_output",
                            content=[dict(type="text", text=json.dumps(dict(results=results)))],
                        )
                    ],
                    usage=dict(total_input_tokens=100, total_output_tokens=100),
                )
            ).encode(),
            10,
        )


def test_frozen_review_preserves_aliases_real_audio_and_resumes_without_repaying(monkeypatch, tmp_path):
    j, c = review_fixture(monkeypatch)
    manifest = prepare(j, c)
    assert len(manifest["windows"]) == 2
    transport = Reviewer()
    run(j, c, transport)
    assert len(transport.calls) == 2
    monkeypatch.delenv("JEMINI_API_KEY")
    run(j, c, transport)
    reprocess(j, c, "Replay exact receipt without provider")
    assert len(transport.calls) == 2 and ledger(j, manifest)[0] == 2
    for w in manifest["windows"]:
        value = j.get(w["key"])
        payload = json.loads(j.bytes(w["key"], value["payload_blob"]))
        text = json.dumps(payload)
        assert set(value["case_aliases"]) == set(value["case_ids"])
        assert all(original not in text for original in value["case_aliases"].values())
        assert manifest["source_audio_sha256"] not in text
        assert state(j, w["key"]) == "reviewed" and len(history(j, w["key"])[0]["processing"]) == 2
    status = export(j, c, tmp_path)
    assert status["all_cases_resolved"] and status["counts"]["dismissed"] == 2
    assert status["whole_recording_review"] == "not_performed" and not status["production_ready"]
    decisions = json.loads((tmp_path / "decisions.json").read_text())["decisions"]
    assert {d["candidate_id"] for d in decisions} == set(manifest["expected_case_ids"])
    assert "private-review-key" not in repr(j.records)
    with pytest.raises(Conflict):
        prepare(j, c.model_copy(update=dict(max_requests=9)))


def test_system_refusal_halts_provider_and_retry_is_separate_bounded_attempt(monkeypatch, tmp_path):
    j, c = review_fixture(monkeypatch, max_requests=3)
    manifest = prepare(j, c)
    refusal = Reviewer(HttpReceipt(503, {}, b'{"error":"service unavailable"}', 10))
    run(j, c, refusal)
    run(j, c, refusal)
    first, second = manifest["windows"]
    assert (
        len(refusal.calls) == 1
        and state(j, first["key"]) == "provider_rejected"
        and not history(j, second["key"])
    )
    export(j, c, tmp_path)
    exported = json.loads((tmp_path / "decisions.json").read_text())
    assert exported["windows"][1]["blocked_reason"] == "stopped_after_provider_failure"
    good = Reviewer()
    run(j, c, good, retry_window=1, reason="Explicit retry after service recovery")
    rows = history(j, first["key"])
    assert len(rows) == 2 and rows[1]["prepared"]["parent_attempt_ref"] == j.ref(
        rows[0]["key"] + ":prepared"
    ).model_dump(mode="json")
    assert rows[0]["receipt"]["status_code"] == 503 and rows[1]["receipt"]["status_code"] == 200
    run(j, c, good)
    assert len(good.calls) == 2 and ledger(j, manifest)[0] == 3


def test_lost_ack_never_resubmits_or_advances_to_other_windows(monkeypatch):
    j, c = review_fixture(monkeypatch)
    manifest = prepare(j, c)
    transport = Reviewer(lost=True)
    with pytest.raises(RuntimeError):
        run(j, c, transport)
    run(j, c, transport)
    assert len(transport.calls) == 1
    assert state(j, manifest["windows"][0]["key"]) == "outcome_unknown"
    assert not history(j, manifest["windows"][1]["key"])


def test_request_limit_and_wrong_case_output_stay_explicit_without_auto_retry(monkeypatch, tmp_path):
    j, c = review_fixture(monkeypatch, max_requests=1)
    manifest = prepare(j, c)
    transport = Reviewer()
    run(j, c, transport)
    assert len(transport.calls) == 1 and state(j, manifest["windows"][1]["key"]) == "prepared"
    export(j, c, tmp_path)
    exported = json.loads((tmp_path / "decisions.json").read_text())
    assert exported["windows"][1]["blocked_reason"] == "budget_exhausted"
    j2, c2 = review_fixture(monkeypatch)
    bad = Reviewer(
        HttpReceipt(
            200,
            {},
            b'{"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":"{\\"results\\":[]}"}]}]}',
            5,
        )
    )
    run(j2, c2, bad)
    run(j2, c2, bad)
    assert len(bad.calls) == 1
    assert state(j2, prepare(j2, c2)["windows"][0]["key"]) == "response_invalid"
