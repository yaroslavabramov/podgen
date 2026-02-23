"""Phase 7.1: TTS Generation — convert dialogue to audio."""
import io
import logging
import uuid

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.show_bible import ShowBible
from app.models.script_segment import ScriptSegment
from app.models.episode import Episode
from app.pipeline.state import PipelineState
from app.pipeline.phase7_audio.audio_processor import process_audio
from app.services.storage import get_minio_client, upload_file
from app.config import settings
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, step_started, step_completed, audio_segment_generated, progress

logger = logging.getLogger(__name__)


async def generate_audio(state: PipelineState) -> dict:
    """LangGraph node: generate TTS audio for all script segments."""
    run_id = state["generation_run_id"]
    publish_event(run_id, phase_started("phase7_audio", "Generating podcast audio"))
    update_generation_progress(run_id, "phase7_audio", "tts", 82.0)

    async with async_session_factory() as db:
        # Load voice profiles from ShowBible
        voice_map = {}  # speaker -> {voice_id, settings}
        if state.get("show_bible_id"):
            result = await db.execute(select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"])))
            bible = result.scalar_one_or_none()
            if bible and bible.voice_profiles:
                for i, vp in enumerate(bible.voice_profiles):
                    speaker = "A" if i == 0 else "B"
                    voice_map[speaker] = {
                        "voice_id": vp.get("voice_id", ""),
                        "settings": vp.get("settings", {}),
                    }

        # Load all segments in order
        segments = []
        for sid in state.get("script_segment_ids", []):
            result = await db.execute(select(ScriptSegment).where(ScriptSegment.id == uuid.UUID(sid)))
            seg = result.scalar_one_or_none()
            if seg:
                segments.append(seg)

        segments.sort(key=lambda s: (s.act_number, s.scene_number))

        # Generate audio per line
        audio_segments = []
        transcript_entries = []
        current_ms = 0

        publish_event(run_id, step_started("phase7_audio", "tts_generation", "Synthesizing speech"))

        for seg in segments:
            for line in seg.dialogue_json:
                speaker = line.get("speaker", "A")
                text = line.get("text", "")
                if not text.strip():
                    continue

                ssml_hints = line.get("ssml_hints", {})

                try:
                    audio_data = await _synthesize_line(text, speaker, voice_map, ssml_hints)
                    if audio_data:
                        audio_segments.append(audio_data)
                        duration_ms = _estimate_duration_ms(text)
                        transcript_entries.append({
                            "start_ms": current_ms,
                            "end_ms": current_ms + duration_ms,
                            "speaker": speaker,
                            "text": text,
                        })
                        current_ms += duration_ms + 400  # pause between lines
                except Exception:
                    logger.warning("TTS failed for line: %s...", text[:50], exc_info=True)

            publish_event(run_id, audio_segment_generated(seg.act_number, seg.scene_number))

        publish_event(run_id, step_completed("phase7_audio", "tts_generation"))

        # Post-process audio
        publish_event(run_id, step_started("phase7_audio", "audio_processing", "Processing and normalizing audio"))

        if audio_segments:
            final_audio = process_audio(audio_segments)
            # Upload to MinIO
            client = get_minio_client()
            object_name = f"episodes/{run_id}/episode.mp3"
            upload_file(client, final_audio, object_name, "audio/mpeg")
            duration_seconds = current_ms // 1000
        else:
            object_name = None
            duration_seconds = 0
            final_audio = b""

        publish_event(run_id, step_completed("phase7_audio", "audio_processing"))

        # Create or update Episode record for this run (idempotent resume/retry).
        run_uuid = uuid.UUID(run_id)
        existing_result = await db.execute(
            select(Episode).where(Episode.generation_run_id == run_uuid)
        )
        episode = existing_result.scalar_one_or_none()

        if episode is None:
            episode = Episode(
                id=uuid.uuid4(),
                generation_run_id=run_uuid,
                project_id=uuid.UUID(state["project_id"]),
                title=state.get("user_prompt", "Podcast Episode")[:500],
                language=state["language"],
                duration_seconds=duration_seconds,
                audio_file_path=object_name,
                transcript_json=transcript_entries,
            )
            db.add(episode)
        else:
            episode.project_id = uuid.UUID(state["project_id"])
            episode.title = state.get("user_prompt", "Podcast Episode")[:500]
            episode.language = state["language"]
            episode.duration_seconds = duration_seconds
            episode.audio_file_path = object_name
            episode.transcript_json = transcript_entries
            # Fresh audio run should not keep stale shownotes/quality from a failed prior attempt.
            episode.shownotes_json = None
            episode.quality_metrics = None

        await db.commit()
        episode_id = str(episode.id)

    publish_event(run_id, progress(92.0, f"Audio generated: {duration_seconds // 60}m {duration_seconds % 60}s"))
    update_generation_progress(run_id, "phase7_audio", "tts_done", 92.0)

    return {"episode_id": episode_id, "current_phase": "phase7_audio"}


async def _synthesize_line(text: str, speaker: str, voice_map: dict, ssml_hints: dict) -> bytes | None:
    """Synthesize a single line using ElevenLabs."""
    voice_config = voice_map.get(speaker, {})
    voice_id = voice_config.get("voice_id")

    if not voice_id or not settings.elevenlabs_api_key:
        # Return silence placeholder
        return _generate_silence(len(text) * 10)

    try:
        from elevenlabs import ElevenLabs
        client = ElevenLabs(api_key=settings.elevenlabs_api_key)

        voice_settings = voice_config.get("settings", {})

        audio_iter = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id="eleven_v3",
            voice_settings={
                "stability": voice_settings.get("stability", 0.5),
                "similarity_boost": voice_settings.get("similarity_boost", 0.75),
                "style": voice_settings.get("style", 0.3),
            },
        )

        chunks = []
        for chunk in audio_iter:
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception:
        logger.warning("ElevenLabs TTS failed, using silence", exc_info=True)
        return _generate_silence(len(text) * 10)


def _generate_silence(duration_ms: int) -> bytes:
    """Generate silence as a placeholder when TTS is unavailable."""
    from pydub import AudioSegment
    silence = AudioSegment.silent(duration=duration_ms)
    buf = io.BytesIO()
    silence.export(buf, format="mp3", bitrate="320k")
    return buf.getvalue()


def _estimate_duration_ms(text: str) -> int:
    """Estimate speech duration from text length. ~150 words/min."""
    words = len(text.split())
    return int((words / 150) * 60 * 1000)
