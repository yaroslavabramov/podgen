from podgen.audio.media.processing import decode
from podgen.audio.media.wav_repair import repair_fish_stream
from podgen.audio.synthesis.contracts import SynthesizedAudio
from podgen.domain.errors import ContractError

from ..attempts import history


def select(journal, cell, batch_key, allow_repair):
    rows = history(journal, cell)
    errors = [r["failure"] for row in rows for r in row["processing"] if r["failure"]]
    for row in rows:
        for revision in reversed(row["processing"]):
            key = revision["key"] + ":decoded"
            decoded = journal.get(key)
            if decoded:
                return key, decoded, errors
        audio = journal.get(row["key"] + ":audio")
        receipt = row["receipt"]
        if not allow_repair or row["prepared"]["profile"]["synthesis"]["provider"] != "fish" or not audio:
            continue
        key = batch_key + ":repair:" + cell["cell_id"]
        saved = journal.get(key)
        if saved:
            return key, saved, errors
        try:
            original = journal.bytes(row["key"] + ":audio", audio["provider_audio_blob"])
            repaired, audit = repair_fish_stream(
                original,
                complete=bool(receipt and receipt["complete"] and not receipt["error"]),
                status_code=receipt["status_code"] if receipt else 0,
                sample_rate_hz=audio["sample_rate_hz"],
            )
            pcm, metrics = decode(
                SynthesizedAudio(
                    repaired,
                    "audio/wav",
                    audio["sample_rate_hz"],
                    "wav",
                    audio["request_id"],
                    audio["usage"],
                    audio["usage_basis"],
                )
            )
            value = dict(
                contract="audio-listening-repaired-decoded/1",
                pcm_blob=journal.blob(pcm, "audio/wav"),
                metrics=metrics,
                repaired_container_blob=journal.blob(repaired, "audio/wav"),
                repair=audit,
                provider_output_ref=journal.ref(row["key"] + ":audio").model_dump(mode="json"),
                receipt_ref=journal.ref(row["key"] + ":receipt").model_dump(mode="json"),
            )
            journal.put(key, value)
            return key, value, errors
        except ContractError as error:
            errors.append(dict(phase="listening_container_repair", reason=str(error)))
    return None, None, errors
