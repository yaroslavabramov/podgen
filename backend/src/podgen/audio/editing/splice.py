"""Exact frame splice. Only replacement audio is resampled/gained; parent PCM is copied."""

import hashlib
import io
import math
import tempfile
import wave
from pathlib import Path

from podgen.audio.assembly.pcm import read
from podgen.audio.media.processing import measure, run
from podgen.domain.errors import ContractError


def render(audio, parent_params, normalization):
    channels, width, rate = parent_params
    codec = {2: "pcm_s16le", 3: "pcm_s24le", 4: "pcm_s32le"}[width]
    with tempfile.TemporaryDirectory(prefix="podgen-local-insert-") as folder:
        source, converted, target = (
            Path(folder) / name for name in ("source.wav", "converted.wav", "insert.wav")
        )
        source.write_bytes(audio)
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
                str(channels),
                "-ar",
                str(rate),
                "-c:a",
                codec,
                str(converted),
            ]
        )
        measured = measure(converted)
        ceiling = float(normalization.get("target_true_peak_dbtp", -1))
        requested = float(normalization.get("applied_gain_db", 0))
        gain = min(requested, ceiling - float(measured["input_tp"]) - 0.1)
        if not math.isfinite(gain):
            raise ContractError("local_replacement_nonfinite_gain")
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
                "-c:a",
                codec,
                str(target),
            ]
        )
        result = target.read_bytes()
        final = measure(target)
        if float(final["input_tp"]) > ceiling + 0.05 or read(result)[0] != parent_params:
            raise ContractError("local_replacement_render_format_or_peak_mismatch")
    return result, dict(
        contract="audio-local-insert-transform/1",
        parent_params=list(parent_params),
        requested_source_gain_db=requested,
        applied_gain_db=gain,
        target_true_peak_dbtp=ceiling,
        true_peak_dbtp=float(final["input_tp"]),
        fades=False,
        tempo_changed=False,
        parent_audio_modified=False,
        resample_scope="replacement_only",
    )


def splice(parent, replacements):
    params, frames, pcm = read(parent)
    stride = params[0] * params[1]
    cursor, output, mapping, new_cursor = 0, [], [], 0
    for item in replacements:
        first, last = item["start_frame"], item["end_frame"]
        inserted_params, inserted_frames, inserted_pcm = read(item["audio"])
        if inserted_params != params or not cursor <= first < last <= frames:
            raise ContractError("local_replacement_splice_overlap_or_format_mismatch")
        prefix = pcm[cursor * stride : first * stride]
        if prefix:
            output.append(prefix)
            mapping.append(
                dict(
                    kind="parent_audio",
                    source_start_frame=cursor,
                    source_end_frame=first,
                    master_start_frame=new_cursor,
                    master_end_frame=new_cursor + first - cursor,
                )
            )
            new_cursor += first - cursor
        output.append(inserted_pcm)
        mapping.append(
            dict(
                kind="replacement_audio",
                proposal_id=item["proposal_id"],
                source_start_frame=0,
                source_end_frame=inserted_frames,
                master_start_frame=new_cursor,
                master_end_frame=new_cursor + inserted_frames,
            )
        )
        new_cursor += inserted_frames
        cursor = last
    if cursor < frames:
        output.append(pcm[cursor * stride :])
        mapping.append(
            dict(
                kind="parent_audio",
                source_start_frame=cursor,
                source_end_frame=frames,
                master_start_frame=new_cursor,
                master_end_frame=new_cursor + frames - cursor,
            )
        )
    raw = b"".join(output)
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setparams((*params, 0, "NONE", "not compressed"))
        stream.writeframes(raw)
    result = target.getvalue()
    if read(result)[2] != raw:
        raise ContractError("local_replacement_pcm_splice_verification_failed")
    return (
        result,
        mapping,
        dict(
            contract="exact-approved-range-splice/1",
            parent_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
            master_pcm_sha256=hashlib.sha256(raw).hexdigest(),
            unmodified_ranges_bit_identical=True,
            sample_frames=len(raw) // stride,
            sample_rate_hz=params[2],
            fades=False,
            global_normalization=False,
        ),
    )
