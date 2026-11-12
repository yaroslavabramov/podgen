"""Only overlapping intervals merge; no artificial timing or hidden extra coverage."""


def group(candidates, rate, max_seconds, max_cases):
    windows, excluded = [], []
    for case in sorted(
        candidates,
        key=lambda c: (c["audio_range"]["start_frame"], c["audio_range"]["end_frame"], c["candidate_id"]),
    ):
        start, end = case["audio_range"]["start_frame"], case["audio_range"]["end_frame"]
        if case["clip_status"] != "extracted" or end - start > max_seconds * rate:
            excluded.append(dict(case_id=case["candidate_id"], reason="insufficient_window", candidate=case))
            continue
        previous = windows[-1] if windows else None
        if (
            previous
            and start <= previous["end_frame"]
            and max(end, previous["end_frame"]) - previous["start_frame"] <= max_seconds * rate
            and len(previous["cases"]) < max_cases
        ):
            previous["end_frame"] = max(end, previous["end_frame"])
            previous["cases"].append(case)
        else:
            windows.append(dict(start_frame=start, end_frame=end, cases=[case]))
    return windows, excluded
