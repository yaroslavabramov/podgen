import uuid
from datetime import datetime

from app.ingestion.scout import _build_heading_clusters, _build_source_summaries, _extract_lead, _extract_term_hints
from app.models.chunk import Chunk


def _chunk(
    source_id: uuid.UUID,
    *,
    content: str,
    heading_path: list[str],
    chunk_index: int,
    token_count: int,
) -> Chunk:
    return Chunk(
        source_id=source_id,
        content=content,
        heading_path=heading_path,
        chunk_index=chunk_index,
        token_count=token_count,
    )


def test_build_source_summaries_groups_chunks_per_source() -> None:
    source_a = uuid.uuid4()
    source_b = uuid.uuid4()

    rows = [
        (
            _chunk(
                source_a,
                content="Hong Kong currency began as silver. Later reforms reshaped issuance.",
                heading_path=["History", "Colonial Era"],
                chunk_index=0,
                token_count=120,
            ),
            source_a,
            "HK Currency History",
            "web",
            "https://example.com/hk-history",
            datetime(2020, 5, 1),
        ),
        (
            _chunk(
                source_a,
                content="In 1983 the linked exchange rate system was introduced to stabilize confidence.",
                heading_path=["History", "Modern Era"],
                chunk_index=1,
                token_count=180,
            ),
            source_a,
            "HK Currency History",
            "web",
            "https://example.com/hk-history",
            datetime(2020, 5, 1),
        ),
        (
            _chunk(
                source_b,
                content="HKMA oversees note issuance and monetary stability functions.",
                heading_path=[],
                chunk_index=0,
                token_count=90,
            ),
            source_b,
            "HKMA Overview",
            "web",
            "https://example.com/hkma-overview",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)

    assert len(summaries) == 2
    primary = summaries[0]
    assert primary["source_id"] == str(source_a)
    assert primary["chunk_count"] == 2
    assert primary["total_tokens"] == 300
    assert primary["section_count"] == 2
    assert primary["structure_depth"] == 2
    assert primary["published_at"] == "2020-05-01"
    assert len(primary["sections"]) == 2
    assert primary["sections"][0]["lead"]


def test_extract_lead_prefers_informative_sentence() -> None:
    text = (
        "Short sentence. "
        "This is a longer and more informative sentence that should be selected as the lead. "
        "Another sentence follows."
    )

    lead = _extract_lead(text)

    assert lead.startswith("This is a longer and more informative sentence")
    assert len(lead) <= 260


def test_extract_term_hints_from_headings_and_leads() -> None:
    source_summaries = [
        {
            "sections": [
                {
                    "headings": ["Currency Board Mechanism"],
                    "lead": "Currency board stability depends on reserve backing.",
                    "token_count": 120,
                },
                {
                    "headings": ["Hong Kong Dollar"],
                    "lead": "Dollar peg stability remains central to policy.",
                    "token_count": 80,
                },
            ]
        }
    ]

    term_hints = _extract_term_hints(source_summaries, top_n=10)

    terms = {item["term"] for item in term_hints}
    assert "currency" in terms
    assert "stability" in terms
    assert "the" not in terms


def test_build_heading_clusters_deduplicates_across_sources() -> None:
    source_summaries = [
        {
            "source_id": "s1",
            "sections": [{"headings": ["History", "Note Issuance"], "lead": "A"}],
        },
        {
            "source_id": "s2",
            "sections": [{"headings": ["Banking", "Note Issuance"], "lead": "B"}],
        },
        {
            "source_id": "s3",
            "sections": [{"headings": ["Banking", "Currency Board"], "lead": "C"}],
        },
    ]

    clusters = _build_heading_clusters(source_summaries, top_n=10)

    assert len(clusters) == 1
    assert clusters[0]["heading"] == "Note Issuance"
    assert clusters[0]["source_count"] == 2
    assert clusters[0]["mentions"] == 2
