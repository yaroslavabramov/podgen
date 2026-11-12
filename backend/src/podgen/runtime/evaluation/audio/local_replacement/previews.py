"""Listening excerpts are copied from the final assembled master."""

from podgen.audio.media.recognition import clip


def excerpts(journal, master, rate, frames, start, end, seconds, proposal_id):
    radius = round(rate * seconds)

    def excerpt(first, last):
        body, extraction = clip(master, max(0, first - radius), min(frames, last + radius))
        return dict(
            proposal_id=proposal_id,
            audio_blob=journal.blob(body, "audio/wav"),
            extraction=extraction,
            listening_status="not_reviewed",
        )

    audition = excerpt(start, end)
    joins = [
        dict(excerpt(boundary, boundary), side=side, at_master_frame=boundary)
        for side, boundary in (("start", start), ("end", end))
    ]
    return audition, joins
