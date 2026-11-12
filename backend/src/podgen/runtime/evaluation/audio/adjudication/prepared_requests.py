from podgen.audio.fidelity.adjudication import openai, realtime
from podgen.audio.media.review_projection import project

from .contracts import ReviewConfigV3


def fallback_request(journal, config, prompt, payload, audio):
    if isinstance(config, ReviewConfigV3):
        projected, proof = project(audio)
        request, wire = realtime.prepare_projected(
            prompt, payload, projected, config.fallback.reviewer, proof
        )
        return dict(
            request=request,
            wire_blob=journal.blob(wire, "application/json"),
            transport_audio_blob=journal.blob(projected, "audio/wav"),
            audio_projection=proof,
        )
    request, wire = openai.prepare(prompt, payload, audio, config.fallback.reviewer)
    return dict(request=request, wire_blob=journal.blob(wire, "application/json"))
