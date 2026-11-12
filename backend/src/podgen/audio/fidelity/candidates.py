from difflib import SequenceMatcher
from uuid import uuid5

from .timing import clip_range, map_timing
from .tokens import VERSION, expected_tokens, tokens

VERSION_CANDIDATES = "literal-fidelity-candidates/1"


def generate(audit_id, utterances, recognition, rate, frames, *, margin_seconds=1.5, max_clip_seconds=45):
    expected, actual = expected_tokens(utterances), tokens(recognition.text)
    mapping, issues = map_timing(recognition, actual, rate, frames)
    candidates = []
    matcher = SequenceMatcher(
        a=[t["normalized"] for t in expected], b=[t["normalized"] for t in actual], autojunk=False
    )
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation == "equal":
            continue
        selected = expected[i1:i2]
        candidates.append(
            dict(
                kind="script_discrepancy",
                operation=operation,
                expected_token_range=[i1, i2],
                recognized_token_range=[j1, j2],
                expected_tokens=selected,
                recognized_tokens=actual[j1:j2],
                expected_context=utterances[
                    max(0, (selected[0]["utterance_index"] if selected else 0) - 1) : min(
                        len(utterances),
                        (selected[-1]["utterance_index"] if selected else len(utterances) - 1) + 2,
                    )
                ],
                script_selectors=[
                    dict(
                        utterance_id=t["utterance_id"],
                        utterance_index=t["utterance_index"],
                        char_start=t["char_start"],
                        char_end=t["char_end"],
                    )
                    for t in selected
                ],
            )
        )
    for issue in issues:
        candidates.append(
            dict(
                kind="recognition_uncertainty",
                issue=issue,
                recognized_token_range=issue.get("transcript_token_range", [0, len(actual)]),
                script_selectors=[],
            )
        )
    for i, candidate in enumerate(candidates):
        j1, j2 = candidate["recognized_token_range"]
        window = clip_range(j1, j2, mapping, frames, round(margin_seconds * rate))
        usable = window["end_frame"] - window["start_frame"] <= max_clip_seconds * rate
        candidate.update(
            candidate_id=str(uuid5(audit_id, f"candidate:{i}")),
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
        edit_distance_diagnostic=sum(
            max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
        ),
        expected_token_count=len(expected),
        audio_review_status="pending",
        production_ready=False,
        decision="candidates_generated",
    )
