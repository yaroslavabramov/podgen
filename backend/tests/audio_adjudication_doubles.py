"""Mechanical provider doubles; never acoustic qualification evidence."""

import json

from podgen.audio.http import HttpReceipt
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfigV2
from tests.test_audio_adjudication_runtime import review_fixture


def configuration(old, **overrides):
    raw = old.model_dump(mode="json")
    raw.update(
        contract="audio-fidelity-adjudication/2",
        fallback=dict(
            policy="gemini-to-openai-on-availability/1",
            reservation_per_request_usd="0.20",
            price=dict(
                usd_per_million_input_text_tokens="2.5",
                usd_per_million_input_audio_tokens="32",
                usd_per_million_output_text_tokens="10",
                source_url="https://developers.openai.com/api/docs/models/gpt-audio-1.5",
                checked_at="2026-11-12",
            ),
        ),
        **overrides,
    )
    return ReviewConfigV2.model_validate(raw)


def openai_response(wire, status="dismissed"):
    payload = json.loads(json.loads(wire)["messages"][2]["content"][0]["text"])
    results = [
        dict(
            case_id=c["case_id"],
            status=status,
            reason_code="unclear_speech" if status == "inconclusive" else "asr_error",
            heard_spans=[]
            if status == "inconclusive"
            else [dict(start_seconds=0, end_seconds=0.1, words="слова")],
            explanation="Mechanical fixture.",
        )
        for c in payload["cases"]
    ]
    return json.dumps(
        dict(
            id="openai-fixture",
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(dict(results=results))))],
            usage=dict(
                prompt_tokens=1047,
                completion_tokens=103,
                prompt_tokens_details=dict(text_tokens=927, audio_tokens=120, cached_tokens=0),
                completion_tokens_details=dict(text_tokens=103, audio_tokens=0),
            ),
        )
    ).encode()


class Routed:
    def __init__(self, primary=None, secondary_status="dismissed"):
        self.calls = []
        self.primary = primary or HttpReceipt(503, {}, b"unavailable", 1)
        self.secondary_status = secondary_status

    def submit(self, request, wire, credential):
        self.calls.append((request, wire, credential))
        if "openai.com" in request["url"]:
            return HttpReceipt(200, {}, openai_response(wire, self.secondary_status), 1)
        return self.primary


def setup(monkeypatch, **overrides):
    j, old = review_fixture(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-key")
    return j, configuration(old, **overrides)
