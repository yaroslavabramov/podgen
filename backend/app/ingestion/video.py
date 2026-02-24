"""Video/audio transcript extraction using Deepgram."""

import logging

from app.config import settings
from app.models.source import Source
from app.services.storage import get_minio_client, download_file, get_presigned_url

logger = logging.getLogger(__name__)


async def extract_video(source: Source, language: str | None = None) -> str:
    """Extract transcript from a video or audio source using Deepgram nova-3.

    Supports two modes:
      - File-based: source.file_path points to a file in MinIO.
      - URL-based:  source.original_url points to a publicly accessible media file.

    Args:
        source: The Source object with file_path or original_url.
        language: BCP-47 language code (e.g. "en", "ru"). If None, Deepgram
                  auto-detects the language.

    Returns:
        Formatted transcript as a markdown string.
    """
    if not settings.deepgram_api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is not configured")

    from deepgram import DeepgramClient, PrerecordedOptions

    dg = DeepgramClient(settings.deepgram_api_key)

    opts: dict = {
        "model": "nova-3",
        "smart_format": True,
        "diarize": True,
        "paragraphs": True,
    }
    if language:
        opts["language"] = language

    options = PrerecordedOptions(**opts)

    if source.file_path:
        transcript = await _transcribe_from_file(dg, source.file_path, options)
    elif source.original_url:
        transcript = await _transcribe_from_url(dg, source.original_url, options)
    else:
        raise ValueError(
            f"Source {source.id} has neither file_path nor original_url for transcription"
        )

    if not transcript or not transcript.strip():
        raise ValueError(f"Deepgram returned empty transcript for source {source.id}")

    return transcript


async def _transcribe_from_file(
    dg,
    file_path: str,
    options,
) -> str:
    """Download file from MinIO and send to Deepgram."""
    # Use a presigned URL so Deepgram can fetch it directly (avoids
    # downloading the full file into memory for large media).
    minio = get_minio_client()

    try:
        presigned = get_presigned_url(minio, file_path, expires_hours=1)
        return await _transcribe_from_url(dg, presigned, options)
    except Exception:
        # Fallback: download bytes and send directly
        logger.info("Presigned URL failed for %s, downloading bytes", file_path)
        audio_bytes = download_file(minio, file_path)
        return await _transcribe_bytes(dg, audio_bytes, options)


async def _transcribe_from_url(
    dg,
    url: str,
    options,
) -> str:
    """Send a URL to Deepgram for transcription."""
    from deepgram import UrlSource

    source = UrlSource(url=url)
    response = await dg.listen.asyncrest.v("1").transcribe_url(source, options)
    return _format_transcript(response)


async def _transcribe_bytes(
    dg,
    audio_bytes: bytes,
    options,
) -> str:
    """Send raw bytes to Deepgram for transcription."""
    from deepgram import BufferSource

    source = BufferSource(buffer=audio_bytes)
    response = await dg.listen.asyncrest.v("1").transcribe_file(source, options)
    return _format_transcript(response)


def _format_transcript(response) -> str:
    """Convert Deepgram response into a readable markdown transcript.

    Uses paragraphs from the response when available, otherwise falls back
    to a simple concatenation of transcript alternatives.
    """
    result = response.results

    # Try paragraphs first (from the paragraphs feature)
    try:
        paragraphs = result.channels[0].alternatives[0].paragraphs
        if paragraphs and paragraphs.paragraphs:
            return _format_paragraphs(paragraphs.paragraphs)
    except (AttributeError, IndexError):
        pass

    # Fallback: plain transcript text
    try:
        transcript = result.channels[0].alternatives[0].transcript
        if transcript:
            return transcript
    except (AttributeError, IndexError):
        pass

    raise ValueError("Deepgram response contained no transcript data")


def _format_paragraphs(paragraphs) -> str:
    """Format Deepgram paragraphs with speaker labels into markdown."""
    lines: list[str] = []
    current_speaker: int | None = None

    for para in paragraphs:
        speaker = getattr(para, "speaker", None)

        if speaker is not None and speaker != current_speaker:
            current_speaker = speaker
            lines.append(f"\n**Speaker {speaker}:**\n")

        sentences = getattr(para, "sentences", [])
        para_text = " ".join(s.text for s in sentences if hasattr(s, "text"))
        if para_text:
            lines.append(para_text)

    return "\n\n".join(lines).strip()
