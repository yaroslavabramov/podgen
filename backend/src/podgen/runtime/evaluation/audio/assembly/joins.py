from podgen.audio.media.recognition import clip


def build_joins(journal, config, mapping, source_frames, source_rate, master, master_rate, master_frames):
    result = []
    for i, left in enumerate(mapping[:-1]):
        boundary = left["assembly_end_frame"]
        start = max(0, boundary - round(config.join_context_seconds * source_rate))
        end = min(source_frames, boundary + round(config.join_context_seconds * source_rate))
        first, last = (
            max(0, round(start * master_rate / source_rate)),
            min(master_frames, round(end * master_rate / source_rate)),
        )
        audio, extraction = clip(master, first, last)
        result.append(
            dict(
                join_index=i + 1,
                at_master_frame=left["master_end_frame"],
                at_assembly_frame=boundary,
                left_part_result_ref=left["source_result_ref"],
                right_part_result_ref=mapping[i + 1]["source_result_ref"],
                assembly_range=dict(start_frame=start, end_frame=end),
                master_extraction=extraction,
                audio_blob=journal.blob(audio, "audio/wav"),
                review_status="pending_actual_audio_review",
            )
        )
    return result
