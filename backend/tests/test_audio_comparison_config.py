import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from podgen.audio.synthesis.registry import adapter
from podgen.audio.synthesis.transport import HttpSynthesisTransport
from podgen.domain.canonical import canonical_bytes
from podgen.runtime.evaluation.audio.contracts import load_comparison_config
from podgen.runtime.evaluation.audio.matrix import full_comparison_complete
from tests.audio_cases import config, profile, source


def targeted():
    raw = config().model_dump(mode="json")
    raw["providers"] = ["gemini"]
    raw.update(contract="audio-comparison/2", expected_voices_per_provider={"gemini": 1})
    raw["profiles"][0]["synthesis"] = profile("gemini").model_dump(mode="json")
    raw["fragments"] = raw["fragments"][:1]
    return raw


def test_legacy_roundtrip_and_matrix_stay_exact_with_default_timeout():
    original = config(["elevenlabs", "gemini", "inworld", "fish", "minimax"])
    raw = original.model_dump(mode="json")
    loaded = load_comparison_config(json.dumps(raw))
    assert canonical_bytes(loaded.model_dump(mode="json")) == canonical_bytes(raw)
    assert loaded.expected_samples == 20 and loaded.http_timeout_seconds == 180
    assert "request_timeout_seconds" not in raw and "expected_voices_per_provider" not in raw
    assert full_comparison_complete(dict(configuration=raw), 20)
    assert not full_comparison_complete(dict(configuration=raw), 4)
    raw.pop("contract")
    assert load_comparison_config(json.dumps(raw)) == original


def test_targeted_one_voice_one_fragment_and_timeout_are_explicit():
    raw = targeted()
    raw["request_timeout_seconds"] = 600
    value = load_comparison_config(json.dumps(raw))
    assert value.expected_samples == 1 and value.voice_counts == {"gemini": 1}
    assert value.http_timeout_seconds == 600
    assert not full_comparison_complete(dict(configuration=raw), 1)
    assert load_comparison_config(json.dumps(targeted())).http_timeout_seconds == 180


@pytest.mark.parametrize(
    "change",
    [
        dict(contract="audio-comparison/1"),
        dict(contract="audio-comparison/3"),
        dict(expected_voices_per_provider={}),
        dict(expected_voices_per_provider={"gemini": 1, "fish": 1}),
        dict(expected_voices_per_provider={"gemini": 0}),
        dict(request_timeout_seconds=0),
        dict(request_timeout_seconds=1801),
    ],
)
def test_invalid_or_ambiguous_targeted_matrix_is_rejected(change):
    with pytest.raises(ValidationError):
        load_comparison_config(json.dumps(dict(targeted(), **change)))


def test_more_candidates_than_declared_are_rejected():
    raw = targeted()
    extra = dict(raw["profiles"][0], profile_id=str(uuid4()))
    raw["profiles"].append(extra)
    with pytest.raises(ValidationError, match="exceed_expected"):
        load_comparison_config(json.dumps(raw))


def test_configured_http_timeout_reaches_httpx_and_timeout_does_not_retry(monkeypatch):
    calls, settings = [], []
    real_client = httpx.Client

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"] == dict(connect=600, read=600, write=600, pool=600)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    def factory(**kwargs):
        settings.append(kwargs)
        return real_client(timeout=kwargs["timeout"], transport=httpx.MockTransport(handler))

    monkeypatch.setattr("podgen.audio.synthesis.transport.httpx.Client", factory)
    receipt = HttpSynthesisTransport(timeout_seconds=600).submit(
        adapter("gemini").prepare(profile("gemini"), source()), "test-secret"
    )
    assert len(calls) == 1 and settings[0]["follow_redirects"] is False
    assert not receipt.complete and receipt.error == "ReadTimeout"
