import httpx
import pytest
from podgen.audio.synthesis.contracts import MODELS
from podgen.audio.synthesis.registry import adapter
from podgen.audio.synthesis.transport import HttpSynthesisTransport, encode_request
from podgen.domain.errors import ContractError
from pydantic import ValidationError

from tests.audio_cases import profile, response, signal, source


@pytest.mark.parametrize("provider", list(MODELS))
def test_pinned_models_verbatim_text_lossless_response_and_usage(provider):
    port = adapter(provider)
    speech = source()
    p = profile(provider)
    request = port.prepare(p, speech)
    assert request.model_id == MODELS[provider]
    assert speech.utterances[0].text in str(request.body)
    assert "style" not in str(request.body) and "REASONING_ON" not in str(request.body)
    assert "secret" not in encode_request(request).decode()
    output = port.decode(request, response(provider, signal()))
    assert output.audio == signal() and output.encoding == "wav"
    if provider == "elevenlabs":
        assert output.alignment["alignment"]["characters"] == ["Т"]
    if provider == "fish":
        assert request.headers["model"] == "drama-3"
    if provider == "gemini":
        assert request.body["input"][0]["content"][0]["annotations"] == [dict(type="speech_metadata")]
        assert "language" not in str(request.body)


@pytest.mark.parametrize("provider", list(MODELS))
def test_no_model_fallback_no_unknown_options_no_unvalidated_formats(provider):
    with pytest.raises(ValidationError):
        profile(provider).model_copy(update={"model_id": "default"}).__class__.model_validate(
            dict(profile(provider).model_dump(), model_id="default")
        )
    with pytest.raises(ContractError, match="unsupported_synthesis_option"):
        adapter(provider).prepare(profile(provider, options={"hidden_reasoning": True}), source())
    with pytest.raises(ContractError):
        adapter(provider).prepare(profile(provider, sample_rate_hz=123456), source())


def test_fish_without_model_header_cannot_be_decoded_and_inworld_limits_utf16_sum():
    fish = adapter("fish")
    request = fish.prepare(profile("fish"), source())
    with pytest.raises(ContractError):
        fish.decode(request.model_copy(update={"headers": {}}), response("fish", signal()))
    with pytest.raises(ContractError, match="utf16"):
        adapter("inworld").prepare(profile("inworld"), source("😀" * 1001))


@pytest.mark.parametrize(
    "field,value", [("audio_sample_rate", 16000), ("audio_format", "mp3"), ("audio_channel", 2)]
)
def test_minimax_reports_must_match_requested_audio(field, value):
    import json
    from dataclasses import replace

    reply = response("minimax", signal())
    body = json.loads(reply.body)
    body["extra_info"][field] = value
    with pytest.raises(ContractError, match="format_mismatch"):
        adapter("minimax").decode(
            adapter("minimax").prepare(profile("minimax"), source()),
            replace(reply, body=json.dumps(body).encode()),
        )


def test_transport_archivable_wire_matches_sent_bytes_and_never_retries_or_exposes_key():
    sent = []

    def handler(req):
        sent.append(req)
        assert req.headers["xi-api-key"] == "test-secret"
        return httpx.Response(429, json={"error": "rate limited"})

    request = adapter("elevenlabs").prepare(profile(), source())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = HttpSynthesisTransport(client=client).submit(request, "test-secret")
    assert len(sent) == 1 and sent[0].content == encode_request(request)
    assert result.status_code == 429 and result.complete and "test-secret" not in repr(request)


def test_partial_http_response_keeps_original_bytes_and_unknown_delivery():
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial-audio"
            raise httpx.ReadError("lost connection")

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Broken()))
    ) as client:
        receipt = HttpSynthesisTransport(client=client).submit(
            adapter("fish").prepare(profile("fish"), source()), "not-persisted"
        )
    assert not receipt.complete and receipt.body == b"partial-audio" and receipt.error == "ReadError"
