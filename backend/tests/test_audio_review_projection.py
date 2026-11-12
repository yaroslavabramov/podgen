import io
import wave

import pytest

from podgen.audio.media.review_projection import pcm, project
from podgen.domain.errors import ContractError
from tests.audio_cases import signal


def wav(value=0, *, width=4, rate=24000, seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, width, rate, 0, "NONE", "not compressed"))
        stream.writeframes(value.to_bytes(width, "little", signed=True) * rate * seconds)
    return output.getvalue()


def test_projection_preserves_exact_16bit_signal_and_is_deterministic():
    audio = signal()
    first, proof = project(audio)
    assert project(audio) == (first, proof)
    assert pcm(first)[0] == pcm(audio)[0]
    assert proof["source_sample_frames"] == proof["transport_sample_frames"] == 120000
    assert not proof["source_all_zero"] and not proof["gain_applied"]


def test_resampling_keeps_whole_source_range_and_duration():
    _, proof = project(signal(rate=48000))
    assert proof["source_sample_frames"] == 240000 and proof["transport_sample_frames"] == 120000
    assert proof["source_sample_rate_hz"] == 48000 and proof["transport_sample_rate_hz"] == 24000
    assert proof["source_range"] == dict(start_frame=0, end_frame=240000)


@pytest.mark.parametrize("width", [2, 3, 4])
def test_exact_zero_uses_source_pcm(width):
    _, proof = project(wav(width=width))
    assert proof["source_all_zero"]


def test_nonzero_source_lost_in_quantization_is_explicit_error():
    with pytest.raises(ContractError, match="lost_nonzero_signal"):
        project(wav(1))


def test_quiet_representable_signal_has_no_loudness_gate():
    projected, proof = project(wav(65536))
    assert not proof["source_all_zero"]
    assert pcm(projected)[0] == b"\x01\x00" * 24000
