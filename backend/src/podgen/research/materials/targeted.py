"""Versioned narrow research commission and source-validated compact handoff."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from podgen.domain.errors import ContractError

INSTRUCTIONS = """Investigate only the supplied question in the exact supplied script context. Preserve its actual meaning and qualifications; do not strengthen a claim through paraphrase. The neighboring turns are authoritative context, not claims to research separately. Do not repeat general episode research.
Use primary documents where possible and inspect their actual text. Resolve conflicting accounts explicitly. Return only one JSON object with answer (at most 2000 characters), limitations (at most 2000 characters), and evidence (at most 6 entries). Each evidence entry has url, quote (an exact nonempty contiguous excerpt, at most 1000 characters), and relation (at most 500 characters explaining its relevance). Do not fabricate or normalize quotations. An inaccessible document is a limitation, never evidence. Include the documents in your provider source citations so they can be acquired. Empty evidence is allowed only with an explicit limitation. Use output_language for answer, limitations and relation; preserve source quotes exactly. This is an evidence handoff, not an editorial keep/revise decision."""


class Excerpt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=4096)
    quote: str = Field(min_length=1, max_length=1000)
    relation: str = Field(min_length=1, max_length=500)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=2000)
    limitations: str = Field(max_length=2000)
    evidence: list[Excerpt] = Field(max_length=6)


def parse(text):
    # A provider may wrap its otherwise exact JSON in a Markdown code fence.
    value = text.strip()
    if value.startswith("```json\n") and value.endswith("\n```"):
        value = value[8:-4]
    try:
        from podgen.runtime.evaluation.validation import load_unique_json

        result = Answer.model_validate(load_unique_json(value))
    except (ValidationError, ValueError) as error:
        raise ContractError("targeted_research_invalid_answer") from error
    if not result.evidence and not result.limitations.strip():
        raise ContractError("targeted_research_missing_evidence_or_limitation")
    return result


def compact(answer, sources):
    """Match against retained text, not provider citations or search snippets."""
    from podgen.research.hosted.corpus_acquisition import document_url

    from .resolver import same_document

    evidence = []
    for row in answer.evidence:
        try:
            canonical = document_url(row.url)
        except (ValueError, ContractError) as error:
            raise ContractError("targeted_research_invalid_evidence_url") from error
        source = next((s for url, s in sources if same_document(url, canonical)), None)
        if source is None:
            raise ContractError("targeted_research_evidence_not_retained")
        # VerifiedText.find uses the retained immutable representation.
        start = source.text.find(row.quote)
        if start < 0:
            raise ContractError("targeted_research_quote_not_in_source")
        evidence.append(
            dict(
                source_id=source.source_id,
                quote=row.quote,
                start=start,
                end=start + len(row.quote),
                relation=row.relation,
            )
        )
    return dict(
        contract="targeted-research-answer/1",
        answer=answer.answer,
        limitations=answer.limitations,
        excerpts=evidence,
        relationship="validated_source_locations_not_read_receipts",
        next_action="Read these exact source ranges with read_source to obtain E evidence receipts.",
    )
