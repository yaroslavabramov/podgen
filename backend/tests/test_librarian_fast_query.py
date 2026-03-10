import json
import uuid
from types import SimpleNamespace

import pytest

from app.models.evidence import EvidenceSpan
from app.models.episode_manifest import EpisodeManifest
from app.models.source import AuthorityType, DomainReliability, IngestionStatus, Source, SourceType
from app.pipeline.phase3_research.librarian_fast import (
    _build_search_queries,
    _build_targeted_query,
    _extra_query_terms,
    _rerank_search_results,
    _select_excerpt_windows,
    _select_gap_branches,
    research_fast_web,
)
from app.pipeline.phase3_research.tools import create_web_evidence


def test_build_targeted_query_appends_asset_keywords() -> None:
    question = "How does the HKD issuance mechanism work?"
    missing_assets = ["CASE", "STATISTIC"]

    targeted = _build_targeted_query(question, missing_assets)

    assert targeted.startswith(question)
    assert "case" in targeted.lower()
    assert "statistics" in targeted.lower()


def test_build_targeted_query_extracts_non_ascii_named_tokens() -> None:
    question = "Почему HKMA держал долларовый коридор в 1983 году?"

    targeted = _build_targeted_query(question, [])

    assert "HKMA" in targeted
    assert "1983" in targeted


@pytest.mark.asyncio
async def test_build_search_queries_adds_targeted_variant_only_when_distinct() -> None:
    question = "What happened in 1983?"
    queries = await _build_search_queries(
        question,
        question_intent="history",
        missing_assets=["TIMELINE"],
        podcast_language="en",
    )

    assert queries[0] == question
    assert len(queries) == 2
    assert queries[1] != question


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
                "english_query": "Hong Kong dollar silver standard early colonial period",
                "english_entity_query": "Hong Kong silver standard 1863",
                "english_official_query": "Hong Kong dollar silver standard official history",
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
        podcast_language="ru",
    )

    assert queries[:3] == [
        "Hong Kong dollar silver standard early colonial period",
        "Hong Kong silver standard 1863",
        "Hong Kong dollar silver standard official history",
    ]
    assert any("1863" in query for query in queries)


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
        return json.dumps(
            {
                "english_query": "Hong Kong note issuance delegated to private banks legal basis official",
                "english_entity_query": "",
                "english_official_query": "",
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
        podcast_language="ru",
        mode="verifier_corroboration",
        gap_origin="verifier",
        claim_statement="The colonial government delegated note issuance to private banks for administrative efficiency.",
        search_objective="Find an independent official source that confirms the legal basis.",
        existing_support_summary="single-source; authority=secondary",
    )

    assert any("legal basis" in query.lower() for query in queries)
    assert any("private banks" in query.lower() for query in queries)


@pytest.mark.asyncio
async def test_build_search_queries_uses_aggregated_claim_contexts_in_verifier_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_call_haiku_structured(**kwargs):
        payload = json.loads(kwargs["prompt"])
        assert len(payload["claim_contexts"]) == 2
        return json.dumps(
            {
                "english_query": "Hong Kong note issuance legal basis and banknote committee role official",
                "english_entity_query": "",
                "english_official_query": "",
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


def test_extra_query_terms_add_official_hints_for_verifier_support_strength_tokens() -> None:
    terms = _extra_query_terms(
        "Find independent corroboration for the legal basis.",
        "single_source; 1 source(s); 1 direct / 0 contextual; authority=unknown",
    )

    assert "legal" in [term.lower() for term in terms]
    assert "official" in [term.lower() for term in terms]


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

    evidence_id = await create_web_evidence(
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
    assert evidence_id == str(evidence.id)
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
        return None

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

    evidence_id = await create_web_evidence(
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
    assert evidence_id == str(evidence_rows[0].id)


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
        return [
            {"url": "https://example.com/a", "title": "A", "score": 0.9, "query_text": queries[0]},
            {"url": "https://example.com/b", "title": "B", "score": 0.8, "query_text": queries[0]},
        ]

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
        return str(uuid.uuid4())

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
        return [
            {"url": "https://known.example.com/article", "title": "Known", "score": 0.9, "query_text": queries[0]},
            {"url": "https://novel.example.com/article", "title": "Novel", "score": 0.8, "query_text": queries[0]},
        ]

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
        return str(uuid.uuid4())

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
                }
            ],
        }
    )

    assert fetch_calls == ["https://novel.example.com/article"]
    assert created_urls == ["https://novel.example.com/article"]
    assert result["iteration_counts"]["fast_web_verifier_gap_count"] == 1
