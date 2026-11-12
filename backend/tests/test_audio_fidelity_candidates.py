import io
import wave
from uuid import uuid4

import pytest
from pydantic import ValidationError

from podgen.audio.fidelity.candidates import generate
from podgen.audio.fidelity.tokens import tokens
from podgen.audio.media.recognition import clip, lossless_flac
from podgen.audio.recognition.contracts import Recognition, RecognitionProfile
from podgen.audio.recognition.fish import parse, prepare
from tests.audio_cases import signal


def recognition(text, segments=None, duration=5):
    return Recognition.model_validate(
        dict(
            text=text,
            duration=duration,
            segments=segments
            if segments is not None
            else [dict(text=t["text"], start=i * 0.4, end=i * 0.4 + 0.3) for i, t in enumerate(tokens(text))],
            request_id="test-asr",
        )
    )


def report(expected, recognized, **kwargs):
    return generate(
        uuid4(),
        [dict(utterance_id=str(uuid4()), speaker="HOST", text=expected)],
        recognized,
        24000,
        120000,
        **kwargs,
    )


def test_punctuation_case_and_yo_are_equivalent_but_numbers_negation_and_forms_survive():
    assert not report("Ёлка, Аполлон-13!", recognition("елка аполлон 13"))["candidates"]
    for expected, actual in [("Не 12 недель", "12 недель"), ("3.5", "35"), ("−12", "12"), ("почва", "почвы")]:
        candidate = report(expected, recognition(actual))["candidates"][0]
        assert (
            candidate["kind"] == "script_discrepancy"
            and candidate["conclusion"] == "candidate_only_not_confirmed_tts_error"
        )
        assert candidate["expected_tokens"][0]["text"] in expected
        assert candidate["script_selectors"]


def test_segments_normalizing_decimal_create_uncertainty_even_if_transcript_matches_script():
    result = report(
        "Не 3.5 недели",
        recognition(
            "Не 3.5 недели",
            [dict(text=t, start=i, end=i + 0.5) for i, t in enumerate(["Не", "35", "недели"])],
        ),
    )
    assert all(c["kind"] == "recognition_uncertainty" for c in result["candidates"])
    issue = result["recognition_uncertainties"][0]
    assert issue["transcript_tokens"][0]["text"] == "3.5" and issue["segment_tokens"][0]["text"] == "35"
    assert result["audio_review_status"] == "pending"


def test_missing_timestamps_and_long_unknown_ranges_are_explicitly_inconclusive():
    value = recognition("Слова есть", [], duration=120)
    result = generate(
        uuid4(),
        [dict(utterance_id=str(uuid4()), speaker="HOST", text="Слова есть")],
        value,
        24000,
        120 * 24000,
    )
    assert result["candidates"] and result["recognition_uncertainties"]
    assert result["candidates"][0]["clip_status"] == "range_exceeds_review_limit"
    assert result["candidates"][0]["status"] == "inconclusive"
    assert result["candidates"][0]["audio_range"]["end_frame"] == 120 * 24000


def test_bad_segment_timeline_and_duration_are_not_silently_accepted():
    result = report("Текст", recognition("Текст", [dict(text="Текст", start=8, end=9)], duration=9))
    assert {i["kind"] for i in result["recognition_uncertainties"]} == {
        "invalid_segment_timeline",
        "reported_duration_mismatch",
    }
    for item in result["candidates"]:
        assert 0 <= item["audio_range"]["start_frame"] < item["audio_range"]["end_frame"] <= 120000
    with pytest.raises(ValidationError):
        recognition("Текст", [dict(text="Текст", start=2, end=1)])


def test_lossless_flac_and_exact_frame_clip_preserve_payload():
    original = signal(extremes=True)
    flac, proof = lossless_flac(original)
    assert flac.startswith(b"fLaC") and proof["pcm_bit_identical"]
    assert proof["original_pcm_sha256"] == proof["transport_decoded_pcm_sha256"]
    assert proof["submitted_range"] == dict(start_frame=0, end_frame=120000)
    body, extraction = clip(original, 100, 1000)
    with wave.open(io.BytesIO(original), "rb") as source:
        source.setpos(100)
        expected = source.readframes(900)
    with wave.open(io.BytesIO(body), "rb") as selected:
        assert selected.getnframes() == 900 and selected.readframes(900) == expected
    assert extraction["sample_frames"] == 900


def test_fish_request_is_exact_unprompted_multipart_with_model_header_and_raw_parse():
    request, body = prepare(RecognitionProfile(), b"fLaC-fixture")
    assert request["headers"]["model"] == "transcribe-1-pro"
    assert request["fields"] == dict(
        language="ru", ignore_timestamps="false", diarize="false", tag_audio_events="false"
    )
    assert b"fLaC-fixture" in body and b'filename="audio.flac"' in body
    assert b"prompt" not in body and b"hint" not in body and b"authorization" not in body
    raw = b'{"text":"3.5", "duration":1, "segments":[{"text":"35","start":0,"end":1}],"request_id":"exact-id","other":"preserved in receipt"}'
    parsed = parse(raw)
    assert parsed.text == "3.5" and parsed.segments[0].text == "35"
