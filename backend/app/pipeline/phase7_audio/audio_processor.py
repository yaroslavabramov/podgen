"""Audio post-processing: concatenate, normalize, fade."""

import io
import logging

from pydub import AudioSegment

logger = logging.getLogger(__name__)

LUFS_TARGET = -16
FADE_IN_MS = 2000
FADE_OUT_MS = 3000
PAUSE_BETWEEN_LINES_MS = 400


def process_audio(audio_segments: list[bytes]) -> bytes:
    """Concatenate audio segments, normalize, add fades. Returns MP3 bytes."""
    if not audio_segments:
        return b""

    combined = AudioSegment.empty()
    pause = AudioSegment.silent(duration=PAUSE_BETWEEN_LINES_MS)

    for i, segment_bytes in enumerate(audio_segments):
        try:
            seg = AudioSegment.from_mp3(io.BytesIO(segment_bytes))
            combined += seg
            if i < len(audio_segments) - 1:
                combined += pause
        except Exception:
            logger.warning("Failed to process audio segment %d", i, exc_info=True)
            continue

    if len(combined) == 0:
        return b""

    # Normalize loudness (approximate LUFS normalization)
    target_dbfs = LUFS_TARGET + 3  # rough conversion LUFS → dBFS
    change_db = target_dbfs - combined.dBFS
    combined = combined.apply_gain(change_db)

    # Fade in/out
    combined = combined.fade_in(min(FADE_IN_MS, len(combined) // 2))
    combined = combined.fade_out(min(FADE_OUT_MS, len(combined) // 2))

    # Export as MP3 320kbps
    buf = io.BytesIO()
    combined.export(buf, format="mp3", bitrate="320k")
    return buf.getvalue()
