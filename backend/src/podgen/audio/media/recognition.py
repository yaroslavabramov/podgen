"""Lossless ASR transport and exact-frame review excerpts, without VAD or tempo changes."""

import hashlib
import io
import tempfile
import wave
from pathlib import Path

from podgen.domain.errors import ContractError

from .processing import probe, run


def raw_pcm(path):
    return run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-f", "s32le", "-"]
    ).stdout


def lossless_flac(audio):
    with tempfile.TemporaryDirectory(prefix="podgen-asr-") as folder:
        source, target = Path(folder) / "source.wav", Path(folder) / "transport.flac"
        source.write_bytes(audio)
        info = probe(source)
        if info["codec_name"] not in ("pcm_s16le", "pcm_s24le", "pcm_s32le") or int(info["channels"]) != 1:
            raise ContractError("recognition_requires_single_narrator_pcm")
        original = raw_pcm(source)
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(source),
                "-map_metadata",
                "-1",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s32",
                str(target),
            ]
        )
        decoded = raw_pcm(target)
        if not original or len(original) % 4 or original != decoded:
            raise ContractError("recognition_transport_changed_pcm")
        output_info = probe(target)
        rate = int(info["sample_rate"])
        if int(output_info["sample_rate"]) != rate or int(output_info["channels"]) != 1:
            raise ContractError("recognition_transport_changed_format")
        return target.read_bytes(), dict(
            contract="recognition-lossless-transport/1",
            sample_rate_hz=rate,
            sample_frames=len(original) // 4,
            channels=1,
            source_audio_sha256=hashlib.sha256(audio).hexdigest(),
            transport_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            original_pcm_sha256=hashlib.sha256(original).hexdigest(),
            transport_decoded_pcm_sha256=hashlib.sha256(decoded).hexdigest(),
            pcm_bit_identical=True,
            submitted_range=dict(start_frame=0, end_frame=len(original) // 4),
        )


def clip(audio, start, end):
    with wave.open(io.BytesIO(audio), "rb") as source:
        if not 0 <= start < end <= source.getnframes():
            raise ContractError("fidelity_clip_outside_audio")
        source.setpos(start)
        pcm = source.readframes(end - start)
        width, channels, rate = source.getsampwidth(), source.getnchannels(), source.getframerate()
    if len(pcm) != (end - start) * width * channels:
        raise ContractError("fidelity_clip_truncated")
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        output.writeframes(pcm)
    return target.getvalue(), dict(
        start_frame=start,
        end_frame=end,
        sample_frames=end - start,
        sample_rate_hz=rate,
        pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        extraction="exact_pcm_frames",
    )
