import uuid
from datetime import datetime

from app.ingestion.scout import _build_heading_clusters, _build_source_summaries, _extract_lead
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


def test_extract_lead_skips_lowercase_overlap_like_fragment() -> None:
    text = (
        "and this is likely a carry-over fragment from previous overlap context. "
        "The Hong Kong Monetary Authority oversees note issuance and monetary stability."
    )

    lead = _extract_lead(text)

    assert lead.startswith("The Hong Kong Monetary Authority")


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


def test_build_source_summaries_keeps_root_only_chunks_separate() -> None:
    source_id = uuid.uuid4()
    rows = [
        (
            _chunk(
                source_id,
                content="Intro line. " + ("alpha " * 80),
                heading_path=[],
                chunk_index=0,
                token_count=120,
            ),
            source_id,
            "Long Unstructured Source",
            "web",
            "https://example.com/flat",
            None,
        ),
        (
            _chunk(
                source_id,
                content="Second section. " + ("beta " * 80),
                heading_path=[],
                chunk_index=1,
                token_count=110,
            ),
            source_id,
            "Long Unstructured Source",
            "web",
            "https://example.com/flat",
            None,
        ),
        (
            _chunk(
                source_id,
                content="Third section. " + ("gamma " * 80),
                heading_path=[],
                chunk_index=2,
                token_count=100,
            ),
            source_id,
            "Long Unstructured Source",
            "web",
            "https://example.com/flat",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)
    summary = summaries[0]

    assert summary["chunk_count"] == 3
    assert summary["section_count"] == 3
    assert len(summary["sections"]) == 3
    assert all(section["headings"] == [] for section in summary["sections"])


def test_build_source_summaries_uses_adaptive_section_limit_for_large_source() -> None:
    source_id = uuid.uuid4()
    rows = []
    for idx in range(26):
        rows.append(
            (
                _chunk(
                    source_id,
                    content=f"Section {idx}. " + ("alpha " * 80),
                    heading_path=[],
                    chunk_index=idx,
                    token_count=120,
                ),
                source_id,
                "Large Source",
                "web",
                "https://example.com/large",
                None,
            )
        )

    summaries = _build_source_summaries(rows)
    summary = summaries[0]

    assert summary["chunk_count"] == 26
    assert summary["section_count"] == 26
    # Adaptive cap with few sources allows more than the old hard limit of 12.
    assert len(summary["sections"]) > 12
