import uuid
from datetime import datetime

from app.ingestion.scout import (
    _build_heading_clusters,
    _build_source_summaries,
    _extract_lead,
    _is_navigation_noise_section,
    _strip_overlap_prefix_for_lead,
)
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


def test_build_source_summaries_strips_overlap_before_extracting_lead() -> None:
    source_id = uuid.uuid4()
    prev_chunk = (
        "Article 65 The previous system of establishing advisory bodies by the executive authorities "
        "shall be maintained."
    )
    current_body = (
        "Article 66 The Legislative Council of the Hong Kong Special Administrative Region shall be "
        "the legislature of the Region."
    )
    overlapped_current = f"{prev_chunk}\n\n{current_body}"

    rows = [
        (
            _chunk(
                source_id,
                content=prev_chunk,
                heading_path=["Chapter IV Political Structure", "Section 2 The Executive Authorities"],
                chunk_index=0,
                token_count=120,
            ),
            source_id,
            "Basic Law",
            "web",
            "https://example.com/basic-law",
            None,
        ),
        (
            _chunk(
                source_id,
                content=overlapped_current,
                heading_path=["Chapter IV Political Structure", "Section 3 The Legislature"],
                chunk_index=1,
                token_count=130,
            ),
            source_id,
            "Basic Law",
            "web",
            "https://example.com/basic-law",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)
    sections = summaries[0]["sections"]
    section3 = next(item for item in sections if item["headings"][-1] == "Section 3 The Legislature")

    assert section3["lead"].startswith("Article 66 The Legislative Council")
    assert "Article 65" not in section3["lead"]


def test_strip_overlap_prefix_for_lead_keeps_text_when_prefix_mismatch() -> None:
    text = "Article 66 The Legislative Council shall be the legislature of the Region."
    prev = "Unrelated previous chunk text that should not match."

    assert _strip_overlap_prefix_for_lead(text, prev) == text


def test_build_source_summaries_filters_navigation_sections_for_web_source() -> None:
    source_id = uuid.uuid4()
    rows = [
        (
            _chunk(
                source_id,
                content=(
                    "Browse Topics Countries Series Resources About Search Sign in Help "
                    "Privacy Terms Powered by PubFactory"
                ),
                heading_path=["Topics", "Countries", "Series", "Resources"],
                chunk_index=0,
                token_count=1200,
            ),
            source_id,
            "Portal-like Source",
            "web",
            "https://example.com/portal",
            None,
        ),
        (
            _chunk(
                source_id,
                content="The sterling exchange standard evolved through practical colonial administration reforms.",
                heading_path=["Historical Context"],
                chunk_index=1,
                token_count=500,
            ),
            source_id,
            "Portal-like Source",
            "web",
            "https://example.com/portal",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)
    assert len(summaries) == 1

    summary = summaries[0]
    assert summary["section_count"] == 1
    assert len(summary["sections"]) == 1
    assert summary["sections"][0]["headings"] == ["Historical Context"]


def test_build_source_summaries_excludes_navigation_only_web_source() -> None:
    noisy_source = uuid.uuid4()
    clean_source = uuid.uuid4()
    rows = [
        (
            _chunk(
                noisy_source,
                content=(
                    "Sign in Search Browse Topics Countries Series Resources About Help "
                    "Privacy Terms Powered by PubFactory"
                ),
                heading_path=["Topics", "Countries"],
                chunk_index=0,
                token_count=2200,
            ),
            noisy_source,
            "Noisy Portal",
            "web",
            "https://example.com/noisy",
            None,
        ),
        (
            _chunk(
                noisy_source,
                content=(
                    "Table of Contents Keywords Headings Metrics Edit Annotation Character limit "
                    "Save Cancel"
                ),
                heading_path=["Table of Contents", "Metrics"],
                chunk_index=1,
                token_count=1800,
            ),
            noisy_source,
            "Noisy Portal",
            "web",
            "https://example.com/noisy",
            None,
        ),
        (
            _chunk(
                clean_source,
                content="Hong Kong retained monetary autonomy under the Basic Law after 1997.",
                heading_path=["Monetary Autonomy"],
                chunk_index=0,
                token_count=300,
            ),
            clean_source,
            "Clean Source",
            "web",
            "https://example.com/clean",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)
    summary_ids = {item["source_id"] for item in summaries}

    assert str(noisy_source) not in summary_ids
    assert str(clean_source) in summary_ids


def test_build_source_summaries_downweights_navigation_heavy_web_source() -> None:
    noisy_mixed = uuid.uuid4()
    clean = uuid.uuid4()
    rows = [
        (
            _chunk(
                noisy_mixed,
                content=(
                    "Browse Topics Countries Series Resources About Search Sign in Help "
                    "Privacy Terms Powered by PubFactory"
                ),
                heading_path=["Topics", "Countries", "Series", "Resources"],
                chunk_index=0,
                token_count=3400,
            ),
            noisy_mixed,
            "Noisy Mixed Source",
            "web",
            "https://example.com/noisy-mixed",
            None,
        ),
        (
            _chunk(
                noisy_mixed,
                content=(
                    "Colonial reforms established stable exchange mechanisms and improved currency governance."
                ),
                heading_path=["Historical Analysis"],
                chunk_index=1,
                token_count=600,
            ),
            noisy_mixed,
            "Noisy Mixed Source",
            "web",
            "https://example.com/noisy-mixed",
            None,
        ),
        (
            _chunk(
                clean,
                content=(
                    "The linked exchange rate system anchored market expectations and reduced exchange-rate volatility."
                ),
                heading_path=["Linked Exchange Rate System"],
                chunk_index=0,
                token_count=2000,
            ),
            clean,
            "Clean Source",
            "web",
            "https://example.com/clean",
            None,
        ),
    ]

    summaries = _build_source_summaries(rows)
    assert len(summaries) == 2
    assert summaries[0]["source_id"] == str(clean)
    assert summaries[1]["source_id"] == str(noisy_mixed)


def test_navigation_noise_heuristic_keeps_legitimate_metrics_section() -> None:
    section = {
        "headings": ["Metrics"],
        "lead": "We report metrics for latency, throughput, and quality under production load.",
        "token_count": 320,
    }
    assert _is_navigation_noise_section(section) is False


def test_navigation_noise_heuristic_keeps_legitimate_keywords_section() -> None:
    section = {
        "headings": ["Keywords"],
        "lead": "Keywords include currency board, linked exchange rate, and reserve management.",
        "token_count": 250,
    }
    assert _is_navigation_noise_section(section) is False
