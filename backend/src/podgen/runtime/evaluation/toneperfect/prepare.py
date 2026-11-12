import hashlib
import io
import wave
from uuid import NAMESPACE_URL, uuid5

from podgen.audio.toneperfect import prepare as request_for
from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError


def prepare(store, config):
    raw = config.model_dump(mode="json")
    binding = dict(
        contract="toneperfect-trial-binding/1", configuration_hash=digest(raw), run_id=str(config.run_id)
    )
    previous = store.get("binding.json")
    if previous is not None and previous != binding:
        raise Conflict("toneperfect_configuration_changed")
    store.put("binding.json", binding)
    saved = store.get("manifest.json")
    if saved:
        return saved
    prepared = []
    for case in config.cases:
        audio = case.audio_path.read_bytes()
        if hashlib.sha256(audio).hexdigest() != case.audio_sha256 or len(audio) > 10_000_000:
            raise ContractError("toneperfect_audio_identity_or_size_mismatch")
        with wave.open(io.BytesIO(audio), "rb") as source:
            frames, rate, channels, width = (
                source.getnframes(),
                source.getframerate(),
                source.getnchannels(),
                source.getsampwidth(),
            )
            pcm = source.readframes(frames)
        if (
            channels != 1
            or width not in (2, 3, 4)
            or len(pcm) != frames * channels * width
            or not 0.15 <= frames / rate <= 30
        ):
            raise ContractError("toneperfect_requires_complete_short_mono_pcm_wav")
        reserve = (frames + 15 * rate - 1) // (15 * rate)
        key = "podgen-toneperfect-" + str(uuid5(NAMESPACE_URL, str(config.run_id) + ":" + case.case_id))
        request, wire = request_for(audio, case.reference_text, key)
        prepared.append(
            dict(
                case_id=case.case_id,
                idempotency_key=key,
                reservation_credits=reserve,
                duration_seconds=frames / rate,
                sample_frames=frames,
                sample_rate_hz=rate,
                pcm_sha256=hashlib.sha256(pcm).hexdigest(),
                audio_sha256=case.audio_sha256,
                request=request,
                audio_path=f"cases/{case.case_id}/audio.wav",
                wire_path=f"cases/{case.case_id}/wire.bin",
            )
        )
        store.blob(prepared[-1]["audio_path"], audio)
        store.blob(prepared[-1]["wire_path"], wire)
        store.put(f"cases/{case.case_id}/request.json", request)
    total = sum(case["reservation_credits"] for case in prepared)
    if total > config.max_credits:
        raise ContractError("toneperfect_trial_exceeds_credit_cap")
    manifest = dict(
        contract="toneperfect-prepared-trial/1",
        configuration=raw,
        configuration_hash=binding["configuration_hash"],
        cases=prepared,
        reservation_credits=total,
        pricing=dict(
            credits_per_started_seconds=15,
            source_url="https://api.toneperfect.app/docs",
            checked_at="2026-11-12",
        ),
        engine_policy="primary_only",
        qualification_status="not_evaluated",
        production_ready=False,
    )
    store.put("manifest.json", manifest)
    return manifest
