"""Explicit PCM transport projection; digital zero is measured before quantization."""

import hashlib
import io
import tempfile
import wave
from pathlib import Path

from podgen.domain.errors import ContractError

from .processing import run

VERSION = "review-pcm16-mono24k/1"


def pcm(audio):
    with wave.open(io.BytesIO(audio), "rb") as source:
        channels, width, rate, frames = (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
            source.getnframes(),
        )
        data = source.readframes(frames)
    if channels != 1 or width not in (2, 3, 4) or not frames or len(data) != frames * width:
        raise ContractError("review_projection_requires_complete_mono_signed_pcm")
    return data, width, rate, frames


def project(audio):
    original, width, rate, frames = pcm(audio)
    zero = not any(original)
    with tempfile.TemporaryDirectory(prefix="podgen-review-pcm-") as folder:
        source, target = Path(folder) / "source.wav", Path(folder) / "transport.wav"
        source.write_bytes(audio)
        command = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-af",
            "aresample=24000:dither_method=none",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-fflags",
            "+bitexact",
            str(target),
        ]
        run(command)
        projected = target.read_bytes()
    data, target_width, target_rate, target_frames = pcm(projected)
    if (target_width, target_rate) != (2, 24000) or abs(target_frames * rate - frames * 24000) > rate:
        raise ContractError("review_projection_changed_duration")
    if not zero and not any(data):
        raise ContractError("review_projection_lost_nonzero_signal")
    return projected, dict(
        contract=VERSION,
        source_wav_sha256=hashlib.sha256(audio).hexdigest(),
        source_pcm_sha256=hashlib.sha256(original).hexdigest(),
        source_sample_width_bytes=width,
        source_sample_rate_hz=rate,
        source_sample_frames=frames,
        source_all_zero=zero,
        transport_wav_sha256=hashlib.sha256(projected).hexdigest(),
        transport_pcm_sha256=hashlib.sha256(data).hexdigest(),
        transport_sample_width_bytes=2,
        transport_sample_rate_hz=24000,
        transport_sample_frames=target_frames,
        channels=1,
        resampler="ffmpeg_swr",
        dither="none",
        gain_applied=False,
        source_range=dict(start_frame=0, end_frame=frames),
        duration_rounding="within_one_transport_sample",
        edits="none",
    )
