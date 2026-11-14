"""Compact text-length hints, not current audio measurements or acceptance gates.

The Russian solo reference is rounded from three local macOS Milena/130 readings
of one episode: 2216/1204.895, 2310/1255.410, 2466/1333.313 words/seconds.
Counting is whitespace tokens in utterance text, excluding speaker labels. This
calibration is not evidence for other voices, delivery styles, languages or dialogue.
"""

from math import ceil

REFERENCE_WPM = 110


def duration_context(assignment, script=None, *, include_requested=True):
    language = assignment.output_language.lower().replace("_", "-").split("-")[0]
    words = sum(len(row["text"].split()) for row in script["utterances"]) if script is not None else None
    current = None
    if script is not None:
        current = (
            dict(word_count=words)
            if language in ("ru", "en")
            else dict(character_count=sum(len(row["text"]) for row in script["utterances"]))
        )
    result = dict(current_text=current, audio_measured=False)
    if include_requested:
        result["requested"] = assignment.duration.model_dump(mode="json")
    reference = getattr(assignment, "duration_estimate", None)
    if reference is not None:
        duration = assignment.duration
        result.update(
            estimated_word_target=reference.words_for(duration.target_seconds),
            estimated_word_range=dict(
                min=reference.words_for(duration.min_seconds), max=reference.words_for(duration.max_seconds)
            ),
            estimate_basis="Approximate rate from a previous full recording with the selected voice and delivery.",
        )
        if words is not None:
            result["estimated_current_seconds"] = reference.seconds_for(words)
        return result
    if language == "ru" and assignment.format == "solo":
        duration = assignment.duration
        result.update(
            estimated_word_target=duration.target_seconds * REFERENCE_WPM // 60,
            estimated_word_range=dict(
                min=duration.min_seconds * REFERENCE_WPM // 60, max=duration.max_seconds * REFERENCE_WPM // 60
            ),
            estimate_basis="Russian solo reference reading at 110 words/minute",
        )
        if words is not None:
            result["estimated_current_seconds"] = ceil(words * 60 / REFERENCE_WPM)
    return result
