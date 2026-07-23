from __future__ import annotations

import hashlib
import uuid

from app.generation_v2.tools.evidence_receipt_selection import (
    select_receipt_evidence_range,
)


def _receipt(text: str, *, version_id: uuid.UUID) -> dict:
    return {
        "receipt_ref": "receipt:test",
        "source_ref": "S01",
        "source_version_id": str(version_id),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "start_offset": 0,
        "end_offset": len(text),
        "total_chars": len(text),
    }


def _select(text: str, *, quote: str = "", start=None, end=None) -> dict:
    version_id = uuid.uuid4()
    return select_receipt_evidence_range(
        text=text,
        receipt=_receipt(text, version_id=version_id),
        source_ref="S01",
        source_version_id=version_id,
        quote=quote,
        start_offset=start,
        end_offset=end,
        max_chars=3_000,
    )


def test_receipt_quote_selection_normalizes_unicode_whitespace_and_punctuation() -> None:
    passage = "“Réserve\u00a0assets”—back\npolicy."
    text = f"Intro. {passage} End."

    selected = _select(text, quote='"Re\u0301serve assets"-back policy.')

    assert "error" not in selected
    assert text[selected["start_offset"] : selected["end_offset"]] == passage


def test_receipt_quote_selection_does_not_silently_ignore_case() -> None:
    selected = _select("Named Actor approved the policy.", quote="named Actor approved")

    assert selected["error"] == "quote_not_found_in_source_read_receipt"


def test_ambiguous_receipt_quote_returns_real_candidates_for_explicit_selection() -> None:
    text = "Policy anchor.\n\nDifferent context.\n\nPolicy anchor."

    ambiguous = _select(text, quote="Policy anchor.")

    assert ambiguous["error"] == "quote_ambiguous_in_source_read_receipt"
    assert len(ambiguous["candidates"]) == 2
    assert all(
        text[item["start_offset"] : item["end_offset"]] == item["passage_text"]
        for item in ambiguous["candidates"]
    )
    chosen = ambiguous["candidates"][1]
    selected = _select(
        text,
        start=chosen["start_offset"],
        end=chosen["end_offset"],
    )
    assert "error" not in selected
    assert selected["start_offset"] == chosen["start_offset"]
    assert selected["end_offset"] == chosen["end_offset"]


def test_missing_receipt_quote_returns_bounded_real_passages_not_fake_evidence() -> None:
    text = "Opening context.\n\nThe reserve mechanism has a narrow statutory scope.\n\nClosing caveat."

    missing = _select(text, quote="reserve mechanism proves a different causal claim")

    assert missing["error"] == "quote_not_found_in_source_read_receipt"
    assert 1 <= len(missing["candidates"]) <= 3
    assert "window_ref" not in missing
    assert all(
        text[item["start_offset"] : item["end_offset"]] == item["passage_text"]
        for item in missing["candidates"]
    )


def test_long_ambiguous_quote_candidates_remain_selectable_inside_partial_receipt() -> None:
    version_id = uuid.uuid4()
    repeated = "A" * 3_500
    text = f"outside-prefix::{repeated}--gap--{repeated}::outside-suffix"
    receipt_start = len("outside-prefix::")
    receipt_end = len(text) - len("::outside-suffix")
    receipt = {
        **_receipt(text, version_id=version_id),
        "start_offset": receipt_start,
        "end_offset": receipt_end,
    }

    ambiguous = select_receipt_evidence_range(
        text=text,
        receipt=receipt,
        source_ref="S01",
        source_version_id=version_id,
        quote=repeated,
        start_offset=None,
        end_offset=None,
        max_chars=3_000,
    )

    assert ambiguous["error"] == "quote_ambiguous_in_source_read_receipt"
    assert len(ambiguous["candidates"]) == 2
    for candidate in ambiguous["candidates"]:
        assert receipt_start <= candidate["start_offset"] < candidate["end_offset"] <= receipt_end
        selected = select_receipt_evidence_range(
            text=text,
            receipt=receipt,
            source_ref="S01",
            source_version_id=version_id,
            quote="",
            start_offset=candidate["start_offset"],
            end_offset=candidate["end_offset"],
            max_chars=3_000,
        )
        assert "error" not in selected
