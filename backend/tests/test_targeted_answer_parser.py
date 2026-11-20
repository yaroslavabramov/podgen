"""Gemini's saved Sources appendix is presentation, never proof of a quotation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from podgen.domain.errors import ContractError
from podgen.research.materials.targeted import compact, parse, validated_excerpts

SOURCES = (Path(__file__).parent / "fixtures/gemini_deep_research_sources.md").read_text()
# Exact appendix from the retained 2026-11-15 hieroglyphs targeted report.
ANSWER = dict(
    answer="A scoped answer.",
    limitations="",
    evidence=[dict(url="https://example.org/document", quote="exact phrase", relation="The record.")],
)


@pytest.mark.parametrize("fence", [None, "json", ""])
@pytest.mark.parametrize("sources", [False, True])
def test_one_answer_accepts_saved_gemini_presentation(fence, sources):
    text = json.dumps(ANSWER)
    if fence is not None:
        text = f"```{fence}\n{text}\n```"
    if sources:
        text += "\n\n" + SOURCES
    assert parse(text).model_dump() == ANSWER


def test_source_numbers_can_start_later_and_have_gaps():
    trailer = "**Sources:**\n11. [Original](https://example.org/one)\n42. [Record](https://example.org/two)"
    assert parse(json.dumps(ANSWER) + "\n" + trailer).model_dump() == ANSWER


@pytest.mark.parametrize(
    "text",
    [
        '{"answer":"first","answer":"second","limitations":"", "evidence":[]}',
        json.dumps(ANSWER) + "\n" + json.dumps(ANSWER),
        json.dumps(ANSWER) + "\n**Sources:**\n" + json.dumps(ANSWER),
        json.dumps(ANSWER) + "\n" + SOURCES + '\n{"answer":"different"}',
        "A possible answer:\n" + json.dumps(ANSWER),
        "```json\n" + json.dumps(ANSWER) + "\n" + SOURCES,
        json.dumps(ANSWER).replace('"quote":', '"quote":"invented", "quote":'),
    ],
)
def test_ambiguous_or_duplicate_answers_are_rejected(text):
    with pytest.raises(ContractError, match="targeted_research_invalid_answer"):
        parse(text)


def test_literal_matches_are_kept_separately_from_fabricated_quotes():
    value = dict(ANSWER, evidence=[*ANSWER["evidence"], dict(ANSWER["evidence"][0], quote="invented")])
    answer = parse(json.dumps(value) + "\n" + SOURCES)
    sources = [("https://example.org/document", SimpleNamespace(source_id="S1", text="An exact phrase."))]
    excerpts, rejected = validated_excerpts(answer, sources)
    assert excerpts == [dict(source_id="S1", quote="exact phrase", start=3, end=15, relation="The record.")]
    assert rejected == [dict(evidence_index=1, error="targeted_research_quote_not_in_source")]
    with pytest.raises(ContractError, match="targeted_research_quote_not_in_source"):
        compact(answer, sources)
