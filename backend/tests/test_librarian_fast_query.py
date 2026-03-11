import json
import uuid
from types import SimpleNamespace

import pytest

from app.models.evidence import EvidenceSpan
from app.models.episode_manifest import EpisodeManifest
from app.models.source import AuthorityType, DomainReliability, IngestionStatus, Source, SourceType
from app.pipeline.phase3_research.librarian_fast import (
    _build_search_queries,
    _is_low_value_page,
    _normalize_search_query,
    _rerank_search_results,
    _run_search_queries,
    _select_excerpt_windows,
    _select_gap_branches,
    research_fast_web,
)
from app.pipeline.phase3_research.tools import CreatedWebEvidenceResult, create_web_evidence
from app.pipeline.step_control import StepExecutionStopped


@pytest.mark.asyncio
async def test_build_search_queries_uses_llm_synthesis_for_english_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert payload["podcast_language"] == "en"
        assert payload["mode"] == "hunter_gap_fill"
        return json.dumps(
            {
                "queries": [
                    "Hong Kong dollar 1983 peg history",
                    "HKD linked exchange rate 1983 official timeline",
                ]
            }
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    question = "What happened in 1983?"
    queries = await _build_search_queries(
        question,
        question_intent="history",
        missing_assets=["TIMELINE"],
        required_assets=[],
        expected_assets=[],
        podcast_language="en",
    )

    assert queries == [
        "Hong Kong dollar 1983 peg history",
        "HKD linked exchange rate 1983 official timeline",
    ]
    assert all(query != question for query in queries)


@pytest.mark.asyncio
async def test_build_search_queries_falls_back_to_compact_query_on_synthesis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    queries = await _build_search_queries(
        "Почему HKMA держал долларовый коридор в 1983 году?",
        question_intent="history",
        missing_assets=["TIMELINE"],
        required_assets=[],
        expected_assets=[],
        podcast_language="ru",
    )

    assert queries
    assert any("HKMA" in query for query in queries)
    assert any("1983" in query for query in queries)
    assert all(len(query) <= 320 for query in queries)


@pytest.mark.asyncio
async def test_build_search_queries_fallback_keeps_short_sentence_case_question_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    queries = await _build_search_queries(
        "How did it work?",
        question_intent="explain",
        missing_assets=["MECHANISM"],
        required_assets=[],
        expected_assets=[],
        podcast_language="en",
    )

    assert queries == ["How did it work?"]


@pytest.mark.asyncio
async def test_build_search_queries_fallback_caps_long_named_token_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    long_claim = " ".join(f"ORDINANCE{idx:02d}SECTION{idx:02d}" for idx in range(1, 25))

    queries = await _build_search_queries(
        "Почему система работала?",
        question_intent="debate",
        missing_assets=[],
        required_assets=[{"type": "MECHANISM", "min_count": 1}],
        expected_assets=["CONTRAST"],
        podcast_language="ru",
        mode="verifier_corroboration",
        gap_origin="verifier",
        claim_statement=long_claim,
    )

    assert queries
    assert all(len(query) <= 320 for query in queries)


def test_select_gap_branches_returns_all_explicit_gaps_sorted() -> None:
    gaps = _select_gap_branches(
        [],
        [
            {"question_id": "q2", "branch_id": "b2", "question": "Second", "priority": 2},
            {"question_id": "q1", "branch_id": "b1", "question": "First", "priority": 1},
            {"question_id": "q3", "branch_id": "b3", "question": "Third", "priority": 3},
            {"question_id": "q4", "branch_id": "b4", "question": "Fourth", "priority": 1},
        ],
    )

    assert [(gap["branch_id"], gap["question_id"]) for gap in gaps] == [
        ("b1", "q1"),
        ("b4", "q4"),
        ("b2", "q2"),
        ("b3", "q3"),
    ]
    assert all(gap["gap_origin"] == "hunter" for gap in gaps)


def test_select_gap_branches_fallback_keeps_all_priority_branches() -> None:
    branches = [
        {"id": "b1", "question": "First", "priority": 1},
        {"id": "b2", "question": "Second", "priority": 2},
        {"id": "b3", "question": "Third", "priority": 2},
        {"id": "b4", "question": "Fourth", "priority": 3},
    ]

    gaps = _select_gap_branches(branches, None)

    assert [gap["branch_id"] for gap in gaps] == ["b1", "b2", "b3"]


@pytest.mark.asyncio
async def test_build_search_queries_adds_english_rewrite_for_non_english_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        assert "question_intent" in kwargs["prompt"]
        return json.dumps(
            {
                "queries": [
                    "Hong Kong dollar silver standard early colonial period",
                    "Hong Kong silver standard 1863",
                    "Hong Kong dollar silver standard official history",
                ]
            }
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    queries = await _build_search_queries(
        "Что такое серебряный долларовый стандарт и как он функционировал в раннем Гонконге до 1863 года?",
        question_intent="explain",
        missing_assets=[],
        required_assets=[],
        expected_assets=[],
        podcast_language="ru",
    )

    assert queries == [
        "Hong Kong dollar silver standard early colonial period",
        "Hong Kong silver standard 1863",
        "Hong Kong dollar silver standard official history",
    ]


@pytest.mark.asyncio
async def test_build_search_queries_uses_claim_and_objective_in_verifier_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert payload["mode"] == "verifier_corroboration"
        assert "claim_statement" in payload
        assert "search_objective" in payload
        assert "claim_contexts" in payload
        assert payload["missing_assets"] == []
        assert payload["required_assets"] == []
        assert payload["expected_assets"] == []
        return json.dumps(
            {
                "queries": [
                    "Hong Kong note issuance private banks legal basis official",
                    "colonial note issuance private banks ordinance Hong Kong",
                ]
            }
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    queries = await _build_search_queries(
        "Почему правительство делегировало эмиссию банкам?",
        question_intent="debate",
        missing_assets=["MECHANISM"],
        required_assets=[{"type": "MECHANISM", "min_count": 1}],
        expected_assets=["CASE"],
        podcast_language="ru",
        mode="verifier_corroboration",
        gap_origin="verifier",
        claim_statement="The colonial government delegated note issuance to private banks for administrative efficiency.",
        search_objective="Find an independent official source that confirms the legal basis.",
        existing_support_summary="single-source; authority=secondary",
    )

    assert any("legal basis" in query.lower() for query in queries)
    assert any("private banks" in query.lower() for query in queries)
    assert all("почему правительство" not in query.lower() for query in queries)


@pytest.mark.asyncio
async def test_build_search_queries_uses_aggregated_claim_contexts_in_verifier_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert len(payload["claim_contexts"]) == 2
        assert payload["missing_assets"] == []
        assert payload["required_assets"] == []
        assert payload["expected_assets"] == []
        return json.dumps(
            {
                "queries": [
                    "Hong Kong note issuance legal basis banknote committee official",
                    "private banks Hong Kong note issuance committee governance",
                ]
            }
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_haiku_structured",
        _fake_call_haiku_structured,
    )

    queries = await _build_search_queries(
        "Почему частная эмиссия выжила?",
        question_intent="debate",
        missing_assets=["MECHANISM"],
        required_assets=[{"type": "MECHANISM", "min_count": 1}],
        expected_assets=["CASE"],
        podcast_language="ru",
        mode="verifier_corroboration",
        gap_origin="verifier",
        claim_contexts=[
            {
                "claim_id": "c1",
                "claim_statement": "The colonial government delegated note issuance to private banks.",
                "support_strength": "single_source",
                "search_objective": "Find an independent official source that confirms the legal basis.",
                "existing_support_summary": "single_source; authority=secondary",
            },
            {
                "claim_id": "c2",
                "claim_statement": "The Exchange Fund Advisory Committee coordinates note issuance governance.",
                "support_strength": "single_source",
                "search_objective": "Find an independent official source that confirms committee governance.",
                "existing_support_summary": "single_source; authority=secondary",
            },
        ],
    )

    assert any("private banks" in query.lower() for query in queries)
    assert any("committee" in query.lower() for query in queries)


def test_select_excerpt_windows_prefers_relevant_middle_section() -> None:
    text = (
        "Intro boilerplate. " * 120
        + "\n\n"
        + "Historical context without the target terms. " * 80
        + "\n\n"
        + "The colonial government delegated note issuance to private banks under Ordinance 5. "
        * 60
        + "\n\n"
        + "Trailing section. " * 60
    )

    windows = _select_excerpt_windows(
        text,
        gap={
            "question": "Why did the government delegate note issuance to private banks?",
            "claim_statement": "The colonial government delegated note issuance to private banks under Ordinance 5.",
            "search_objective": "Find the legal basis for delegated note issuance.",
            "missing_assets": ["MECHANISM"],
        },
        query_text="delegated note issuance private banks legal basis",
    )

    assert windows
    assert "private banks" in windows[0].lower()
    assert "ordinance 5" in windows[0].lower()


def test_rerank_search_results_penalizes_reused_source_family_across_gaps() -> None:
    ranked = _rerank_search_results(
        [
            {"url": "https://repeat.example.com/article-a", "title": "Repeat", "score": 0.8, "query_text": "query"},
            {"url": "https://novel.example.org/article-b", "title": "Novel", "score": 0.8, "query_text": "query"},
        ],
        gap={
            "question": "Why did the government delegate note issuance to banks?",
            "claim_statement": "The government delegated note issuance to banks under colonial law.",
            "search_objective": "Find an independent corroborating source.",
            "missing_assets": ["MECHANISM"],
        },
        known_web_sources={},
        used_source_families={"repeat.example.com": 1},
    )

    assert ranked[0]["source_family"] == "novel.example.org"
    assert ranked[1]["source_family"] == "repeat.example.com"


def test_is_low_value_page_requires_claim_anchor_overlap_in_verifier_mode() -> None:
    gap = {
        "gap_origin": "verifier",
        "question": "Did the Bank of China become a note issuer after 1997?",
        "claim_statement": "In 1994 HKMA granted Bank of China note-issuing rights.",
    }

    noisy_general_page = (
        "Hong Kong's monetary system remained stable after 1997. "
        "The currency board supports confidence and banking stability. "
    ) * 20
    direct_page = (
        "In 1994 the Hong Kong Monetary Authority approved Bank of China as a note-issuing bank in Hong Kong. "
    ) * 20

    assert _is_low_value_page(
        noisy_general_page,
        gap,
        query_text="Bank of China note issuer Hong Kong 1994 HKMA",
    )
    assert not _is_low_value_page(
        direct_page,
        gap,
        query_text="Bank of China note issuer Hong Kong 1994 HKMA",
    )


def test_normalize_search_query_caps_monster_query_length() -> None:
    long_query = " ".join(["Hong", "Kong", "currency", "board", "legal", "basis"] * 120)

    normalized = _normalize_search_query(long_query)

    assert len(normalized) <= 320
    assert normalized.startswith("Hong Kong currency board legal basis")


@pytest.mark.asyncio
async def test_run_search_queries_keeps_partial_results_when_one_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_web_search_tool(query: str):
        if "bad" in query:
            raise RuntimeError("query too long")
        return [
            {
                "url": f"https://example.com/{query}",
                "title": query,
                "snippet": f"snippet {query}",
                "score": 0.7,
            }
        ]

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.web_search_tool",
        _fake_web_search_tool,
    )

    results, failures = await _run_search_queries(["good-a", "bad-query", "good-b"])

    assert {item["url"] for item in results} == {
        "https://example.com/good-a",
        "https://example.com/good-b",
    }
    assert failures == [
        {
            "query": "bad-query",
            "error": "query too long",
            "error_type": "RuntimeError",
        }
    ]


@pytest.mark.asyncio
async def test_run_search_queries_reraises_step_execution_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_web_search_tool(query: str):
        if query == "stop":
            raise StepExecutionStopped()
        return []

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.web_search_tool",
        _fake_web_search_tool,
    )

    with pytest.raises(StepExecutionStopped):
        await _run_search_queries(["ok", "stop", "never"])


class _FakeAsyncDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_calls = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_create_web_evidence_creates_temp_source_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeAsyncDB()
    project_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    manifest_id = str(uuid.uuid4())
    provenance_calls: list[tuple[str, str, str, str, str | None]] = []

    async def _fake_find_canonical_source_by_url(*args, **kwargs):
        return None

    async def _fake_get_or_create(**kwargs):
        evidence = EvidenceSpan(
            id=uuid.uuid4(),
            project_id=uuid.UUID(kwargs["project_id"]),
            source_id=kwargs["source_id"],
            verbatim_quote=kwargs["verbatim_quote"],
            asset_type=kwargs["asset_type"],
            narrative_value_score=kwargs["narrative_value_score"],
        )
        db.add(evidence)
        await db.flush()
        return evidence, True

    async def _fake_ensure_evidence_provenance(*, context, **kwargs):
        provenance_calls.append(
            (
                context.question_id,
                context.branch_id,
                context.asset_type,
                context.retrieved_by,
                context.direct_or_contextual,
            )
        )
        return object(), True

    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools._find_canonical_source_by_url",
        _fake_find_canonical_source_by_url,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.get_or_create_canonical_evidence",
        _fake_get_or_create,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.ensure_evidence_provenance",
        _fake_ensure_evidence_provenance,
    )

    result = await create_web_evidence(
        url="https://reuters.com/article",
        quote="A concrete quote.",
        asset_type="case",
        project_id=project_id,
        db=db,
        generation_run_id=run_id,
        manifest_id=manifest_id,
        question_id="b6.1",
        branch_id="b6",
        query_text="Why does this matter today?",
        direct_or_contextual="direct",
        title="Reuters coverage",
    )

    sources = [item for item in db.added if isinstance(item, Source)]
    evidence_rows = [item for item in db.added if isinstance(item, EvidenceSpan)]

    assert db.flush_calls >= 2
    assert len(sources) == 1
    assert len(evidence_rows) == 1
    source = sources[0]
    evidence = evidence_rows[0]

    assert source.ingestion_status == IngestionStatus.PENDING
    assert source.is_temporary is True
    assert source.fully_ingested is False
    assert str(source.generation_run_id) == run_id
    assert source.domain_reliability == DomainReliability.MEDIUM
    assert source.authority_type == AuthorityType.SECONDARY
    assert evidence.source_id == source.id
    assert result.evidence_id == str(evidence.id)
    assert result.created_source is True
    assert result.created_evidence is True
    assert result.created_provenance is True
    assert provenance_calls == [("b6.1", "b6", "case", "librarian_fast", "direct")]


@pytest.mark.asyncio
async def test_create_web_evidence_reuses_canonical_source(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeAsyncDB()
    project_id = str(uuid.uuid4())
    canonical_source = Source(
        id=uuid.uuid4(),
        project_id=uuid.UUID(project_id),
        source_type=SourceType.WEB,
        original_url="https://reuters.com/article",
        title="Canonical Reuters coverage",
        ingestion_status=IngestionStatus.COMPLETED,
        is_temporary=False,
        fully_ingested=True,
        domain_reliability=DomainReliability.MEDIUM,
        authority_type=AuthorityType.SECONDARY,
    )

    async def _fake_find_canonical_source_by_url(*args, **kwargs):
        return canonical_source

    async def _fake_get_or_create(**kwargs):
        evidence = EvidenceSpan(
            id=uuid.uuid4(),
            project_id=uuid.UUID(kwargs["project_id"]),
            source_id=kwargs["source_id"],
            verbatim_quote=kwargs["verbatim_quote"],
            asset_type=kwargs["asset_type"],
            narrative_value_score=kwargs["narrative_value_score"],
        )
        db.add(evidence)
        await db.flush()
        return evidence, True

    async def _fake_ensure_evidence_provenance(**kwargs):
        return object(), True

    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools._find_canonical_source_by_url",
        _fake_find_canonical_source_by_url,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.get_or_create_canonical_evidence",
        _fake_get_or_create,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.tools.ensure_evidence_provenance",
        _fake_ensure_evidence_provenance,
    )

    result = await create_web_evidence(
        url="https://reuters.com/article",
        quote="A concrete quote.",
        asset_type="case",
        project_id=project_id,
        db=db,
        generation_run_id=str(uuid.uuid4()),
        manifest_id=str(uuid.uuid4()),
        question_id="b6.1",
        branch_id="b6",
    )

    sources = [item for item in db.added if isinstance(item, Source)]
    evidence_rows = [item for item in db.added if isinstance(item, EvidenceSpan)]

    assert sources == []
    assert len(evidence_rows) == 1
    assert evidence_rows[0].source_id == canonical_source.id
    assert result.evidence_id == str(evidence_rows[0].id)
    assert result.created_source is False
    assert result.created_evidence is True
    assert result.created_provenance is True


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        return self._execute_results.pop(0)

    async def commit(self):
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_research_fast_web_governor_counts_processed_gaps_not_evidence_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda *args, **kwargs: None,
    )

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        return (
            [
                {"url": "https://example.com/a", "title": "A", "score": 0.9, "query_text": queries[0]},
                {"url": "https://example.com/b", "title": "B", "score": 0.8, "query_text": queries[0]},
            ],
            [],
        )

    async def _fake_call_sonnet_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        question = payload["research_question"]
        return json.dumps(
            [
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": f"{question} evidence 1",
                    "asset_type": "TIMELINE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.5,
                    "tellability_notes": "first",
                },
                {
                    "url": "https://example.com/b",
                    "verbatim_quote": f"{question} evidence 2",
                    "asset_type": "TIMELINE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.6,
                    "tellability_notes": "second",
                },
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": f"{question} evidence 3",
                    "asset_type": "TIMELINE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.7,
                    "tellability_notes": "third",
                },
            ]
        )

    created_for_questions: list[str] = []

    async def _fake_create_web_evidence(*, query_text, **kwargs):
        created_for_questions.append(kwargs["question_id"])
        assert query_text == "query"
        return CreatedWebEvidenceResult(
            evidence_id=str(uuid.uuid4()),
            created_source=False,
            created_evidence=True,
            created_provenance=True,
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.create_web_evidence",
        _fake_create_web_evidence,
    )
    async def _fake_load_known_web_source_index(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_web_source_index,
    )

    async def _fake_prepare_gap_pages(*, ranked_results, **kwargs):
        return (
            [
                {
                    "url": ranked_results[0]["url"],
                    "title": ranked_results[0]["title"],
                    "query_text": ranked_results[0]["query_text"],
                    "extraction_profile": "stub",
                    "content_strategy": "relevance_windows",
                    "excerpt_windows": ["Question-aligned evidence window " * 30],
                },
                {
                    "url": ranked_results[1]["url"],
                    "title": ranked_results[1]["title"],
                    "query_text": ranked_results[1]["query_text"],
                    "extraction_profile": "stub",
                    "content_strategy": "relevance_windows",
                    "excerpt_windows": ["Second evidence window " * 30],
                },
            ],
            [],
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._prepare_gap_pages",
        _fake_prepare_gap_pages,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=2,
            max_fast_web_initial_gaps=2,
            max_fast_web_verifier_gaps=2,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )

    result = await research_fast_web(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "language": "ru",
            "evidence_ids": [],
            "iteration_counts": {},
            "research_gaps": [
                {"question_id": "q1", "branch_id": "b1", "question": "Question 1", "priority": 1},
                {"question_id": "q2", "branch_id": "b2", "question": "Question 2", "priority": 1},
                {"question_id": "q3", "branch_id": "b3", "question": "Question 3", "priority": 1},
            ],
        }
    )

    assert result["iteration_counts"]["fast_web_initial_gap_count"] == 2
    assert result["iteration_counts"]["fast_web_gap_count"] == 2
    assert result["iteration_counts"]["fast_web_per_act"] == 2
    assert created_for_questions == ["q1", "q1", "q1", "q2", "q2", "q2"]


@pytest.mark.asyncio
async def test_research_fast_web_skips_known_fully_ingested_urls_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=5,
            max_fast_web_initial_gaps=5,
            max_fast_web_verifier_gaps=5,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        return (
            [
                {"url": "https://known.example.com/article", "title": "Known", "score": 0.9, "query_text": queries[0]},
                {"url": "https://novel.example.com/article", "title": "Novel", "score": 0.8, "query_text": queries[0]},
            ],
            [],
        )

    fetch_calls: list[str] = []

    async def _fake_fetch_page_tool(url):
        fetch_calls.append(url)
        return {
            "content_markdown": "The colonial government delegated note issuance to private banks under Ordinance 5. " * 30,
            "url": url,
            "title": "Novel article",
            "extraction_profile": "trafilatura",
        }

    async def _fake_call_sonnet_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert payload["extractor_mode"] == "claim_corroboration"
        assert payload["required_evidence"] == []
        assert payload["expected_evidence"] == []
        assert payload["target_missing_assets"] == []
        assert "search_objective" not in payload
        assert payload["corroboration_goals"] == ["independent_second_source"]
        assert payload["question_background_assets"]["required_evidence"] == [{"type": "MECHANISM", "min_count": 1}]
        assert payload["claim_contexts"][0]["corroboration_goal"] == "independent_second_source"
        assert "search_objective" not in payload["claim_contexts"][0]
        assert payload["web_pages"][0]["url"] == "https://novel.example.com/article"
        return json.dumps(
            [
                {
                    "url": "https://novel.example.com/article",
                    "verbatim_quote": "The colonial government delegated note issuance to private banks under Ordinance 5.",
                    "asset_type": "MECHANISM",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.6,
                    "tellability_notes": "direct corroboration",
                }
            ]
        )

    created_urls: list[str] = []

    async def _fake_create_web_evidence(*, url, **kwargs):
        created_urls.append(url)
        return CreatedWebEvidenceResult(
            evidence_id=str(uuid.uuid4()),
            created_source=False,
            created_evidence=True,
            created_provenance=True,
        )

    async def _fake_load_known_sources(*args, **kwargs):
        return {
            "known.example.com/article": {
                "source_id": str(uuid.uuid4()),
                "url": "https://known.example.com/article",
                "title": "Known canonical source",
                "fully_ingested": True,
                "chunk_count": 8,
                "has_local_content": True,
                "text_preview": "Known preview",
                "authority_type": "secondary",
                "domain_reliability": "medium",
            }
        }

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_sources,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.fetch_page_tool",
        _fake_fetch_page_tool,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.create_web_evidence",
        _fake_create_web_evidence,
    )

    result = await research_fast_web(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "language": "en",
            "evidence_ids": [],
            "iteration_counts": {},
            "research_gaps": [
                {
                    "question_id": "q1",
                    "branch_id": "b1",
                    "question": "Why did the government delegate note issuance to private banks?",
                    "priority": 1,
                    "gap_origin": "verifier",
                    "claim_statement": "The colonial government delegated note issuance to private banks under Ordinance 5.",
                    "search_objective": "Find an independent official source that confirms the legal basis.",
                    "required_assets": [{"type": "MECHANISM", "min_count": 1}],
                    "expected_assets": ["CASE"],
                    "missing_assets": ["MECHANISM"],
                }
            ],
        }
    )

    assert fetch_calls == ["https://novel.example.com/article"]
    assert created_urls == ["https://novel.example.com/article"]
    assert result["iteration_counts"]["fast_web_verifier_gap_count"] == 1


@pytest.mark.asyncio
async def test_research_fast_web_claim_corroboration_does_not_reject_non_target_asset_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=5,
            max_fast_web_initial_gaps=5,
            max_fast_web_verifier_gaps=5,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        return (
            [
                {"url": "https://novel.example.com/article", "title": "Novel", "score": 0.8, "query_text": queries[0]},
            ],
            [],
        )

    async def _fake_prepare_gap_pages(**kwargs):
        return (
            [
                {
                    "url": "https://novel.example.com/article",
                    "title": "Novel",
                    "query_text": "query",
                    "extraction_profile": "stub",
                    "content_strategy": "relevance_windows",
                    "excerpt_windows": ["Historical corroboration window " * 30],
                }
            ],
            [],
        )

    async def _fake_call_sonnet_structured(**kwargs):
        return json.dumps(
            [
                {
                    "url": "https://novel.example.com/article",
                    "verbatim_quote": "By 1860 around 1,500 to 1,600 banks issued notes in the United States.",
                    "asset_type": "STATISTIC",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.7,
                    "tellability_notes": "direct corroboration",
                }
            ]
        )

    created_asset_types: list[str] = []

    async def _fake_create_web_evidence(*, asset_type, **kwargs):
        created_asset_types.append(asset_type)
        return CreatedWebEvidenceResult(
            evidence_id=str(uuid.uuid4()),
            created_source=False,
            created_evidence=True,
            created_provenance=True,
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._prepare_gap_pages",
        _fake_prepare_gap_pages,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.create_web_evidence",
        _fake_create_web_evidence,
    )
    async def _fake_load_known_sources(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_sources,
    )

    result = await research_fast_web(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "language": "en",
            "evidence_ids": [],
            "iteration_counts": {},
            "research_gaps": [
                {
                    "question_id": "q1",
                    "branch_id": "b3",
                    "question": "What is a currency board and why can private banks issue notes safely?",
                    "priority": 1,
                    "gap_origin": "verifier",
                    "claim_statement": "By 1860 around 1,500 to 1,600 banks issued notes in the United States.",
                    "search_objective": "Find an independent second source that confirms the number of banks.",
                    "required_assets": [{"type": "DEFINITION", "min_count": 1}],
                    "expected_assets": ["CONTRAST"],
                    "missing_assets": ["DEFINITION"],
                }
            ],
        }
    )

    assert created_asset_types == ["statistic"]
    assert len(result["evidence_ids"]) == 1


@pytest.mark.asyncio
async def test_research_fast_web_propagates_step_stop_without_writing_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]
    dump_calls: list[str] = []

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=5,
            max_fast_web_initial_gaps=5,
            max_fast_web_verifier_gaps=5,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )
    async def _fake_load_known_sources(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_sources,
    )

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        raise StepExecutionStopped()

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._dump_fast_web_debug",
        lambda *, dump_type, **kwargs: dump_calls.append(dump_type),
    )

    with pytest.raises(StepExecutionStopped):
        await research_fast_web(
            {
                "generation_run_id": str(uuid.uuid4()),
                "project_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
                "language": "en",
                "evidence_ids": [],
                "iteration_counts": {},
                "research_gaps": [
                    {"question_id": "q1", "branch_id": "b1", "question": "Question 1", "priority": 1}
                ],
            }
        )

    assert dump_calls == []


@pytest.mark.asyncio
async def test_research_fast_web_summary_counts_accepted_vs_new_canonical_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]
    summary_payloads: list[dict] = []

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=5,
            max_fast_web_initial_gaps=5,
            max_fast_web_verifier_gaps=5,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )
    async def _fake_load_known_sources(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_sources,
    )

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        return (
            [{"url": "https://example.com/a", "title": "A", "score": 0.9, "query_text": queries[0]}],
            [],
        )

    async def _fake_prepare_gap_pages(**kwargs):
        return (
            [
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "query_text": "query",
                    "extraction_profile": "stub",
                    "content_strategy": "relevance_windows",
                    "excerpt_windows": ["Relevant corroboration window " * 20],
                }
            ],
            [],
        )

    async def _fake_call_sonnet_structured(**kwargs):
        return json.dumps(
            [
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": "Quote one.",
                    "asset_type": "CASE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.4,
                    "tellability_notes": "one",
                },
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": "Quote two.",
                    "asset_type": "CASE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.5,
                    "tellability_notes": "two",
                },
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": "Quote three.",
                    "asset_type": "CASE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.6,
                    "tellability_notes": "three",
                },
            ]
        )

    results = [
        CreatedWebEvidenceResult(str(uuid.uuid4()), True, True, True),
        CreatedWebEvidenceResult(str(uuid.uuid4()), False, False, True),
        CreatedWebEvidenceResult(str(uuid.uuid4()), False, False, False),
    ]

    async def _fake_create_web_evidence(**kwargs):
        return results.pop(0)

    def _fake_dump_fast_web_debug(*, dump_type, payload, **kwargs):
        if dump_type == "summary":
            summary_payloads.append(payload)

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._prepare_gap_pages",
        _fake_prepare_gap_pages,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.create_web_evidence",
        _fake_create_web_evidence,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._dump_fast_web_debug",
        _fake_dump_fast_web_debug,
    )

    await research_fast_web(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "language": "en",
            "evidence_ids": [],
            "iteration_counts": {},
            "research_gaps": [
                {"question_id": "q1", "branch_id": "b1", "question": "Question 1", "priority": 1}
            ],
        }
    )

    assert summary_payloads
    summary = summary_payloads[0]
    assert summary["accepted_item_count"] == 3
    assert summary["new_evidence_count"] == 1
    assert summary["new_canonical_evidence_count"] == 1
    assert summary["new_provenance_links_count"] == 2
    assert summary["new_linked_evidence_count"] == 2
    assert summary["new_source_count"] == 1


@pytest.mark.asyncio
async def test_research_fast_web_emits_event_for_provenance_only_useful_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = EpisodeManifest(id=uuid.uuid4(), research_tree_json={"branches": []})
    research_session = _FakeSession([_FakeScalarResult(manifest)])
    deep_session = _FakeSession([_FakeScalarResult(0), _FakeListResult([])])
    sessions = [research_session, deep_session]
    events = []

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.async_session_factory",
        lambda: sessions.pop(0),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.publish_event",
        lambda run_id, event: events.append(event),
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.settings",
        SimpleNamespace(
            max_fast_web_per_act=5,
            max_fast_web_initial_gaps=5,
            max_fast_web_verifier_gaps=5,
            max_deep_ingestion_concurrent=0,
            web_extractor_debug_dump_dir="",
        ),
    )

    async def _fake_load_known_sources(*args, **kwargs):
        return {}

    async def _fake_build_search_queries(*args, **kwargs):
        return ["query"]

    async def _fake_run_search_queries(queries):
        return (
            [{"url": "https://example.com/a", "title": "A", "score": 0.9, "query_text": queries[0]}],
            [],
        )

    async def _fake_prepare_gap_pages(**kwargs):
        return (
            [
                {
                    "url": "https://example.com/a",
                    "title": "A",
                    "query_text": "query",
                    "extraction_profile": "stub",
                    "content_strategy": "relevance_windows",
                    "excerpt_windows": ["Relevant corroboration window " * 20],
                }
            ],
            [],
        )

    async def _fake_call_sonnet_structured(**kwargs):
        return json.dumps(
            [
                {
                    "url": "https://example.com/a",
                    "verbatim_quote": "Quote one.",
                    "asset_type": "CASE",
                    "direct_or_contextual": "direct",
                    "narrative_value_score": 0.4,
                    "tellability_notes": "one",
                }
            ]
        )

    async def _fake_create_web_evidence(**kwargs):
        return CreatedWebEvidenceResult(
            evidence_id=str(uuid.uuid4()),
            created_source=False,
            created_evidence=False,
            created_provenance=True,
        )

    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._load_known_web_source_index",
        _fake_load_known_sources,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._build_search_queries",
        _fake_build_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._run_search_queries",
        _fake_run_search_queries,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast._prepare_gap_pages",
        _fake_prepare_gap_pages,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase3_research.librarian_fast.create_web_evidence",
        _fake_create_web_evidence,
    )

    result = await research_fast_web(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "manifest_id": str(manifest.id),
            "language": "en",
            "evidence_ids": [],
            "iteration_counts": {},
            "research_gaps": [
                {"question_id": "q1", "branch_id": "b1", "question": "Question 1", "priority": 1}
            ],
        }
    )

    evidence_events = [event for event in events if getattr(event, "type", "") == "evidence_found"]
    progress_events = [event for event in events if getattr(event, "type", "") == "progress"]

    assert len(result["evidence_ids"]) == 1
    assert evidence_events
    assert evidence_events[-1].data["count"] == 1
    assert progress_events
    assert "Found 1 additional web evidence" in progress_events[-1].data["message"]
