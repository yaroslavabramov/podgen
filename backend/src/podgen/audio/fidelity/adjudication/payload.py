"""Explicit model-facing context; provenance identifiers stay in the immutable manifest."""


def case_payload(case, recognition, source, offset_frames, frames, rate, alias):
    actual = case.get("recognized_tokens", [])
    excerpt = (
        recognition["text"][min(t["char_start"] for t in actual) : max(t["char_end"] for t in actual)]
        if actual
        else ""
    )
    start, end = offset_frames / rate, (offset_frames + frames) / rate
    segments = [
        dict(
            text=s["text"], start_in_window_seconds=s["start"] - start, end_in_window_seconds=s["end"] - start
        )
        for s in recognition["segments"]
        if s["end"] >= start and s["start"] <= end
    ]
    utterance_aliases = {u["utterance_id"]: f"U{i}" for i, u in enumerate(source["source"]["utterances"], 1)}
    return dict(
        case_id=alias,
        window_duration_seconds=frames / rate,
        expected_words=[t["text"] for t in case.get("expected_tokens", [])],
        original_context=[
            dict(utterance=utterance_aliases[u["utterance_id"]], text=u["text"])
            for u in case["expected_context"]
        ],
        asr_transcript_excerpt=excerpt,
        asr_segment_excerpts=segments,
        candidate_kind=case["kind"],
        representation_conflict=(case.get("issue") or {}).get("kind"),
        localization_notes=[i["kind"] for i in case.get("timing_uncertainties", [])],
    )
