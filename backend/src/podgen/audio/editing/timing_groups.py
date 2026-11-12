"""Keep collapsed ASR words attached to a shared, observed onset."""

from podgen.audio.recognition.contracts import Recognition


def collapsed_onsets(recognition):
    segments = recognition.model_dump(mode="json")["segments"]
    grouped, proofs, index = [], [], 0
    while index < len(segments):
        row = segments[index]
        last = index
        if row["start"] == row["end"]:
            while last + 1 < len(segments) and segments[last + 1]["start"] == row["start"]:
                last += 1
                if segments[last]["end"] > row["start"]:
                    break
        if last > index and segments[last]["end"] > row["start"]:
            grouped.append(
                dict(
                    text=" ".join(item["text"] for item in segments[index : last + 1]),
                    start=row["start"],
                    end=segments[last]["end"],
                )
            )
            proofs.append(
                dict(
                    group_index=len(grouped) - 1,
                    source_segment_range=[index, last + 1],
                    basis="collapsed_words_share_observed_following_word_onset",
                    start=row["start"],
                    end=segments[last]["end"],
                    internal_word_times="unavailable",
                )
            )
        else:
            last = index
            grouped.append(row)
        index = last + 1
    return Recognition.model_validate(dict(recognition.model_dump(mode="json"), segments=grouped)), proofs
