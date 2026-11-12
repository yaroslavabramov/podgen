import pytest
from podgen.audio.media.processing import decode, normalize
from podgen.audio.synthesis.contracts import SynthesizedAudio
from podgen.domain.errors import ContractError

from tests.audio_cases import signal


def audio(data):
    return SynthesizedAudio(data, "audio/wav", 24000, "wav", "fixture", {}, "signal-test")


def test_full_pcm16_decode_detects_both_full_scale_extremes_without_calling_them_clipping():
    pcm, metrics = decode(audio(signal(extremes=True)))
    assert metrics["source_bits_per_sample"] == 16 and metrics["full_scale_samples"] == 2
    assert metrics["sample_frames"] == 24000 * 5 and "clipped_samples" not in metrics
    _, ordinary = decode(audio(signal()))
    assert ordinary["full_scale_samples"] == 0
    assert pcm.startswith(b"RIFF")


def test_linear_loudness_normalization_is_measured_and_original_is_preserved():
    original = signal()
    pcm, measured = decode(audio(original))
    normalized, report = normalize(pcm, -20, -1)
    assert original == signal() and normalized != pcm
    assert report["normalization_type"] == "linear" and abs(report["integrated_lufs"] + 20) <= 0.5
    assert report["true_peak_dbtp"] <= -0.8 and measured["full_decode"]


def test_truncated_audio_and_declared_rate_mismatch_fail_instead_of_silence_fallback():
    with pytest.raises(ContractError):
        decode(audio(signal()[:-500]))
    from dataclasses import replace

    with pytest.raises(ContractError, match="sample_rate_mismatch"):
        decode(replace(audio(signal()), sample_rate_hz=16000))
