"""Reprocess a saved recognition receipt into an exact target extraction."""

from podgen.audio.editing.localization import locate
from podgen.audio.media.recognition import clip
from podgen.audio.recognition.fish import parse
from podgen.domain.errors import ContractError


def extract(journal, proposal, root, key, audio, reason):
    number = 1
    while journal.get(f"{root}:extraction:{number}") is not None:
        number += 1
    output = f"{root}:extraction:{number}"
    journal.put(
        output, dict(receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"), reason=reason)
    )
    try:
        receipt = journal.get(key + ":receipt")
        recognition = parse(journal.bytes(key + ":receipt", receipt["raw_response_blob"]))
        journal.put(
            output + ":recognition",
            dict(
                output=recognition.model_dump(mode="json"),
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        payload = proposal["payload"]
        selection = locate(
            payload["synthesis_input"]["utterances"],
            payload["context_selection"]["target_token_range"],
            recognition,
            audio,
        )
        middle, proof = clip(audio, selection["start_frame"], selection["end_frame"])
        journal.put(
            root + ":result",
            dict(
                contract="audio-local-extraction/1",
                proposal_hash=proposal["proposal_hash"],
                contextual_audio_sha256=journal.get(root + ":manifest")["contextual_audio_blob"]["sha256"],
                recognition_ref=journal.ref(output + ":recognition").model_dump(mode="json"),
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
                selection=selection,
                extraction=proof,
                extracted_audio_blob=journal.blob(middle, "audio/wav"),
            ),
        )
        return "extraction_ready"
    except (ValueError, KeyError, TypeError, ContractError) as error:
        failure = dict(
            contract="audio-local-localization-failure/1",
            reason=str(error),
            extraction_ref=journal.ref(output).model_dump(mode="json"),
        )
        journal.put(output + ":failure", failure)
        # The stable failure marker is an immutable first-failure receipt. A later
        # explicitly requested successful reprocess takes precedence in current().
        if journal.get(root + ":failure") is None:
            journal.put(root + ":failure", failure)
        return "localization_failed"
