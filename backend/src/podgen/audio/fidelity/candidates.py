from difflib import SequenceMatcher
from uuid import uuid5

from podgen.domain.canonical import digest

from .context import expected_range, source_context
from .timing import clip_range, map_timing
from .tokens import VERSION, expected_tokens, tokens

VERSION_CANDIDATES = "literal-fidelity-candidates/3"


def generate(audit_id, utterances, recognition, rate, frames, *, margin_seconds=1.5, max_clip_seconds=45):
    expected, actual = expected_tokens(utterances), tokens(recognition.text)
    mapping, issues = map_timing(recognition, actual, rate, frames)
    candidates = []
    matcher = SequenceMatcher(
        a=[t["normalized"] for t in expected], b=[t["normalized"] for t in actual], autojunk=False
    )
    opcodes = matcher.get_opcodes()
    for operation, i1, i2, j1, j2 in opcodes:
        if operation == "equal":
            continue
        candidates.append(
            dict(
                kind="script_discrepancy",
                operation=operation,
                recognized_token_range=[j1, j2],
                recognized_tokens=actual[j1:j2],
                **source_context(utterances, expected, i1, i2),
            )
        )
    for issue in issues:
        if issue["kind"] != "transcript_segment_text_mismatch":
            continue
        j1, j2 = issue.get("transcript_token_range", [0, len(actual)])
        i1, i2 = expected_range(opcodes, j1, j2, len(expected))
        candidates.append(
            dict(
                kind="recognition_uncertainty",
                issue=issue,
                recognized_token_range=[j1, j2],
                recognized_tokens=actual[j1:j2],
                **source_context(utterances, expected, i1, i2),
            )
        )
    for candidate in candidates:
        j1, j2 = candidate["recognized_token_range"]
        window = clip_range(j1, j2, mapping, frames, round(margin_seconds * rate))
        usable = window["end_frame"] - window["start_frame"] <= max_clip_seconds * rate
        localization = [
            issue
            for issue in issues
            if issue["kind"] != "transcript_segment_text_mismatch"
            and (
                "transcript_token_range" not in issue
                or (issue["transcript_token_range"][0] <= j2 and issue["transcript_token_range"][1] >= j1)
            )
        ]
        candidate.update(
            timing_uncertainties=localization,
            candidate_id=str(uuid5(audit_id, "candidate:" + digest(candidate))),
            audio_range=window,
            status="pending_audio_adjudication" if usable else "inconclusive",
            clip_status="ready_for_extraction" if usable else "range_exceeds_review_limit",
            conclusion="candidate_only_not_confirmed_tts_error",
        )
    return dict(
        contract=VERSION_CANDIDATES,
        normalization_contract=VERSION,
        expected_tokens=expected,
        recognized_tokens=actual,
        candidates=candidates,
        recognition_uncertainties=issues,
        localization_status="uncertain" if issues else "mapped",
        edit_distance_diagnostic=sum(
            max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
        ),
        expected_token_count=len(expected),
        audio_review_status="pending",
        production_ready=False,
        decision="candidates_generated",
    )
