import io
import json
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from podgen.domain.errors import ContractError

VERSION = "ffmpeg-lossless-comparison/1"


def run(args):
    result = subprocess.run(args, capture_output=True, timeout=180)
    if result.returncode:
        raise ContractError("audio_processing_failed", result.stderr.decode(errors="replace")[-3000:])
    return result


def wav_pcm16(data, rate):
    if len(data) % 2:
        raise ContractError("truncated_pcm16_response")
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(data)
    return target.getvalue()


def probe(path):
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    streams = json.loads(result.stdout)["streams"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    if len(audio) != 1 or len(streams) != 1:
        raise ContractError("audio_expected_single_stream")
    return audio[0]


def measure(path):
    result = run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "info",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-20:TP=-1:LRA=50:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    log = result.stderr.decode()
    metrics = json.JSONDecoder().raw_decode(log[log.rfind("{") :])[0]
    if metrics["input_i"] in ("-inf", "inf"):
        raise ContractError("audio_signal_silent")
    return metrics


def decode(audio):
    source = wav_pcm16(audio.audio, audio.sample_rate_hz) if audio.encoding == "pcm_s16le" else audio.audio
    with tempfile.TemporaryDirectory(prefix="podgen-decode-") as folder:
        path = Path(folder) / "provider.audio"
        path.write_bytes(source)
        info = probe(path)
        if info["codec_name"] not in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "flac"):
            raise ContractError("provider_audio_is_not_supported_lossless_pcm")
        if int(info["sample_rate"]) != audio.sample_rate_hz:
            raise ContractError("provider_audio_sample_rate_mismatch")
        decoded = Path(folder) / "decoded.wav"
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map_metadata",
                "-1",
                "-c:a",
                "pcm_s32le",
                str(decoded),
            ]
        )
        raw = run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(decoded), "-f", "s32le", "-"]
        ).stdout
        channels = int(info["channels"])
        if not raw or len(raw) % (4 * channels):
            raise ContractError("audio_invalid_decoded_frames")
        samples = np.frombuffer(raw, dtype="<i4")
        metrics = measure(decoded)
        bits = next(
            (
                int(info[k])
                for k in ("bits_per_raw_sample", "bits_per_sample")
                if str(info.get(k, "")).isdigit() and int(info[k]) > 0
            ),
            0,
        )
        if bits not in (16, 24, 32):
            raise ContractError("unsupported_source_pcm_precision")
        positive_limit = ((1 << (bits - 1)) - 1) << (32 - bits)
        report = dict(
            contract=VERSION,
            source_codec=info["codec_name"],
            sample_rate_hz=audio.sample_rate_hz,
            channels=channels,
            sample_frames=len(raw) // (4 * channels),
            duration_seconds=len(raw) / (4 * channels * audio.sample_rate_hz),
            full_decode=True,
            source_bits_per_sample=bits,
            full_scale_samples=int(np.count_nonzero((samples >= positive_limit) | (samples == -2147483648))),
            integrated_lufs=float(metrics["input_i"]),
            true_peak_dbtp=float(metrics["input_tp"]),
            loudness_range_lu=float(metrics["input_lra"]),
        )
        return decoded.read_bytes(), report


def normalize(decoded, target_lufs, true_peak):
    with tempfile.TemporaryDirectory(prefix="podgen-listening-") as folder:
        source = Path(folder) / "source.wav"
        source.write_bytes(decoded)
        converted = Path(folder) / "mono.wav"
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
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s32le",
                str(converted),
            ]
        )
        measured = measure(converted)
        gain = target_lufs - float(measured["input_i"])
        if float(measured["input_tp"]) + gain > true_peak + 0.05:
            raise ContractError("audio_linear_normalization_exceeds_true_peak")
        target = Path(folder) / "blind.wav"
        run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(converted),
                "-map_metadata",
                "-1",
                "-af",
                f"volume={gain}dB",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s24le",
                str(target),
            ]
        )
        final = measure(target)
        if abs(float(final["input_i"]) - target_lufs) > 0.5 or float(final["input_tp"]) > true_peak + 0.2:
            raise ContractError("audio_loudness_target_not_met")
        report = dict(
            contract=VERSION,
            target_lufs=target_lufs,
            target_true_peak_dbtp=true_peak,
            integrated_lufs=float(final["input_i"]),
            true_peak_dbtp=float(final["input_tp"]),
            normalization_type="linear",
            applied_gain_db=gain,
            sample_rate_hz=48000,
            channels=1,
            codec="pcm_s24le",
        )
        return target.read_bytes(), report


def tools():
    if any(shutil.which(name) is None for name in ("ffmpeg", "ffprobe")):
        raise ContractError("audio_ffmpeg_and_ffprobe_required")
    return {name: run([name, "-version"]).stdout.decode().splitlines()[0] for name in ("ffmpeg", "ffprobe")}
