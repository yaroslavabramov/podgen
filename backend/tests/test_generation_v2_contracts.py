from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.generation_v2.agents.editorial import (
    _fallback_act_draft,
    _fallback_claims,
    _fallback_research_memo,
    _fallback_treatment,
    build_act_dossier,
    build_research_memo,
    update_continuity_ledger,
)
from app.generation_v2.citation_validation import validate_citations
from app.generation_v2.finalize import generate_audio_from_segments
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable, _function_calls, _strict_json_schema
from app.generation_v2.schemas import (
    CitationLink,
    ClaimCitationBundle,
    ContinuityLedger,
    DraftClaim,
    FactIssue,
    QuoteCard,
    ResearchMemo,
    SourceCard,
)
from app.generation_v2.source_triage import build_source_triage
from app.generation_v2.source_workspace import _quote_cards_from_chunks
from app.schemas.generation import GenerationCreate
from app.workers.generation_tasks import _make_compare_child


def test_generation_create_defaults_to_v1_full_mode() -> None:
    payload = GenerationCreate(duration_target=1800, user_prompt="Explain a topic")

    assert payload.generation_mode == "v1"
    assert payload.execution_mode == "full"


def test_strict_json_schema_requires_nested_properties_and_blocks_extra_fields() -> None:
    schema = _strict_json_schema(ResearchMemo.model_json_schema())

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"title", "thesis", "narrative_spine", "editorial_atoms"}
    atom_schema = schema["$defs"]["EditorialAtom"]
    assert atom_schema["additionalProperties"] is False
    assert set(atom_schema["required"]) >= {"kind", "title", "explanation", "story_value"}


def test_function_call_parser_accepts_sdk_objects_and_dicts() -> None:
    object_response = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", call_id="call-1", name="search_corpus", arguments='{"query":"x"}')]
    )
    dict_response = SimpleNamespace(
        output=[{"type": "function_call", "call_id": "call-2", "name": "fetch_source_view", "arguments": '{"kind":null}'}]
    )

    assert _function_calls(object_response)[0]["call_id"] == "call-1"
    assert _function_calls(dict_response)[0]["arguments"] == {"kind": None}


def test_writer_tool_policy_excludes_web_search() -> None:
    runtime = V2Runtime()
    tools = runtime._tool_specs(
        vector_store_id="vs_123",
        local_tools=[
            AgentTool(
                name="fetch_source_view",
                description="Fetch source",
                parameters={"type": "object", "properties": {"source_id": {"type": "string"}}},
                handler=None,  # type: ignore[arg-type]
            )
        ],
        policy=ToolPolicy(file_search=True, web_search=False, local_tools=True),
    )

    assert "web_search" not in [tool.get("type") for tool in tools]
    assert "file_search" in [tool.get("type") for tool in tools]


def test_research_tool_policy_can_include_web_search() -> None:
    runtime = V2Runtime()
    tools = runtime._tool_specs(
        vector_store_id=None,
        local_tools=[],
        policy=ToolPolicy(file_search=False, web_search=True, local_tools=False),
    )

    assert [tool.get("type") for tool in tools] == ["web_search"]


def test_fallback_editorial_flow_produces_draft_and_claims() -> None:
    run = SimpleNamespace(
        config={"user_prompt": "Bank note politics", "duration_target": 3600, "language": "en"},
    )
    source_cards = [
        SourceCard(
            source_id=str(uuid.uuid4()),
            title="Central bank report",
            source_type="pdf",
            summary="A report describing the mechanism, controversy, and consequences of currency issuance.",
        )
    ]
    quote_cards = [
        QuoteCard(
            source_id=source_cards[0].source_id,
            quote="The central bank describes several constraints on issuance.",
            locator="p. 1",
            narrative_use="mechanism",
        )
    ]

    memo = _fallback_research_memo(run, source_cards, quote_cards)
    treatment = _fallback_treatment(run, memo, act_count=3)
    dossier = build_act_dossier(
        treatment_act=treatment.acts[0],
        source_cards=source_cards,
        quote_cards=quote_cards,
        ledger=ContinuityLedger(),
    )
    draft = _fallback_act_draft(run, treatment.acts[0], dossier)
    ledger = update_continuity_ledger(ContinuityLedger(), draft)
    bundle = _fallback_claims(source_cards, quote_cards, [draft])

    assert memo.editorial_atoms
    assert treatment.acts[0].source_focus == [source_cards[0].source_id]
    assert len(draft.lines) >= 8
    assert ledger.citation_sensitive_claims
    assert bundle.claims[0].support_level == "supported"
    assert bundle.citation_links[0].source_id == source_cards[0].source_id


def test_fact_issue_contract_includes_act_number() -> None:
    issue = FactIssue(
        claim_id="act-2-core",
        act_number=2,
        issue="Unsupported date claim.",
        repair="Remove the date or cite the source.",
    )

    assert issue.model_dump()["act_number"] == 2
    assert issue.blocking is True


@pytest.mark.asyncio
async def test_citation_validator_blocks_missing_link_for_required_claim() -> None:
    source = _source_card()
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1")],
        citation_links=[],
    )

    report = await validate_citations(None, bundle=bundle, source_cards=[source], quote_cards=[])

    assert report.passed is False
    assert report.missing_required_link_count == 1
    assert report.blocking_issue_count == 1
    assert "no citation link" in report.issues[0].issue


@pytest.mark.asyncio
async def test_citation_validator_blocks_invalid_links_and_weak_support() -> None:
    source = _source_card()
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="weak")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id="unknown-source",
                locator="",
                confidence=0.4,
                supports_span="A specific factual statement",
            ),
            CitationLink(
                claim_id="unknown-claim",
                source_id=source.source_id,
                locator="p. 1",
                confidence=0.9,
            ),
        ],
    )

    report = await validate_citations(None, bundle=bundle, source_cards=[source], quote_cards=[])

    issue_text = " ".join(issue.issue for issue in report.issues)
    assert report.passed is False
    assert report.invalid_link_count >= 2
    assert report.low_confidence_link_count == 1
    assert report.weak_required_claim_count == 1
    assert "unknown claim id" in issue_text
    assert "unknown source id" in issue_text
    assert "locator is empty" in issue_text


@pytest.mark.asyncio
async def test_citation_validator_blocks_quote_mismatch() -> None:
    source = _source_card()
    quote = QuoteCard(
        source_id=source.source_id,
        quote="This exact sentence exists in the prepared quote card.",
        locator="p. 1",
        narrative_use="mechanism",
        representation_id="rep-1",
    )
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source.source_id,
                locator="p. 99",
                quote="A different sentence that is not in the quote cards.",
                representation_id="rep-1",
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(None, bundle=bundle, source_cards=[source], quote_cards=[quote])

    assert report.passed is False
    assert report.quote_mismatch_count == 1
    assert any("could not be found" in issue.issue for issue in report.issues)


@pytest.mark.asyncio
async def test_citation_validator_accepts_valid_quote_card_link() -> None:
    source = _source_card()
    quote = QuoteCard(
        source_id=source.source_id,
        quote="The audit found that the mechanism depends on three concrete constraints.",
        locator="section 2",
        narrative_use="mechanism",
        representation_id="rep-1",
    )
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source.source_id,
                locator="section 2",
                quote=quote.quote,
                representation_id="rep-1",
                quote_card_ref=quote.locator,
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(None, bundle=bundle, source_cards=[source], quote_cards=[quote])

    assert report.passed is True
    assert report.blocking_issue_count == 0
    assert report.quote_match_count == 1


@pytest.mark.asyncio
async def test_citation_validator_allows_only_nonblocking_low_issues() -> None:
    source = _source_card()
    link = CitationLink(
        claim_id="claim-1",
        source_id=source.source_id,
        locator="section 2",
        confidence=0.9,
    )
    blocking_low = FactIssue(
        claim_id="claim-1",
        act_number=1,
        issue="Low severity but explicitly blocking.",
        repair="Repair before publishing.",
        severity="low",
        blocking=True,
    )
    nonblocking_low = blocking_low.model_copy(
        update={"issue": "Low severity and nonblocking.", "blocking": False}
    )

    blocked_report = await validate_citations(
        None,
        bundle=ClaimCitationBundle(
            claims=[_claim("claim-1", support_level="supported")],
            citation_links=[link],
            fact_issues=[blocking_low],
        ),
        source_cards=[source],
        quote_cards=[],
    )
    allowed_report = await validate_citations(
        None,
        bundle=ClaimCitationBundle(
            claims=[_claim("claim-1", support_level="supported")],
            citation_links=[link],
            fact_issues=[nonblocking_low],
        ),
        source_cards=[source],
        quote_cards=[],
    )

    assert blocked_report.passed is False
    assert blocked_report.blocking_issue_count == 1
    assert allowed_report.passed is True
    assert allowed_report.nonblocking_low_issue_count == 1


def test_quote_cards_from_chunks_include_representation_id() -> None:
    source = SimpleNamespace(id=uuid.uuid4())
    chunk = SimpleNamespace(
        content="This is a short lead.\nThe audit found that the mechanism depends on three concrete constraints across the institution and its suppliers.",
        heading_path=["Report", "Mechanism"],
        chunk_index=3,
    )

    cards = _quote_cards_from_chunks(source, [chunk], representation_id="rep-123")

    assert cards[0].representation_id == "rep-123"
    assert cards[0].locator == "Report > Mechanism"


def test_source_triage_ranks_stronger_sources_above_weak_sources() -> None:
    strong = _source_card(
        source_id="strong",
        title="Primary audit on banking mechanism",
        reliability="high",
        authority="primary",
        coverage_status="ready",
        useful_for=["mechanism", "conflict"],
    )
    weak = _source_card(
        source_id="weak",
        title="Unsourced blog",
        reliability="low",
        authority="unknown",
        coverage_status="partial",
        useful_for=["background"],
    )

    triage = build_source_triage(
        source_cards=[weak, strong],
        quote_cards=[],
        user_prompt="banking mechanism conflict",
    )

    assert triage.ranked_sources[0].source_id == "strong"
    assert "strong" in triage.top_sources_by_function["mechanism"]


@pytest.mark.asyncio
async def test_v2_agent_runtime_unavailable_fails_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class RuntimeUnavailable:
        async def call_structured(self, **kwargs):
            raise V2RuntimeUnavailable("no key")

    monkeypatch.setattr(settings, "generation_v2_allow_runtime_fallback", False)
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})

    with pytest.raises(V2RuntimeUnavailable, match="no key"):
        await build_research_memo(
            None,
            run,
            RuntimeUnavailable(),
            source_cards=[],
            quote_cards=[],
            vector_store_id=None,
        )


@pytest.mark.asyncio
async def test_v2_agent_falls_back_only_when_explicitly_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class RuntimeUnavailable:
        async def call_structured(self, **kwargs):
            raise V2RuntimeUnavailable("no key")

        def mark_fallback_used(self, phase: str, agent_name: str) -> None:
            self.fallback = (phase, agent_name)

    monkeypatch.setattr(settings, "generation_v2_allow_runtime_fallback", True)
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})
    source_cards = [
        SourceCard(source_id=str(uuid.uuid4()), title="Source", source_type="pdf", summary="Specific source text.")
    ]
    runtime = RuntimeUnavailable()

    memo = await build_research_memo(
        None,
        run,
        runtime,
        source_cards=source_cards,
        quote_cards=[],
        vector_store_id=None,
    )

    assert memo.risk_register == ["Fallback memo used because provider runtime was unavailable."]
    assert runtime.fallback == ("v2_editorial_research", "research_agent")


@pytest.mark.asyncio
async def test_v2_agent_does_not_mask_prompt_or_schema_errors() -> None:
    class BrokenRuntime:
        async def call_structured(self, **kwargs):
            raise ValueError("schema mismatch")

    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})

    with pytest.raises(ValueError, match="schema mismatch"):
        await build_research_memo(
            None,
            run,
            BrokenRuntime(),
            source_cards=[],
            quote_cards=[],
            vector_store_id=None,
        )


@pytest.mark.asyncio
async def test_runtime_records_prompt_hash_before_runtime_availability_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "")
    runtime = V2Runtime()

    with pytest.raises(V2RuntimeUnavailable):
        await runtime.call_structured(
            db=None,  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_test_phase",
            agent_name="test_agent",
            schema_model=ResearchMemo,
            system="system",
            prompt="prompt body",
        )

    assert runtime.prompt_hash_for("v2_test_phase", "test_agent") == hashlib.sha256(b"prompt body").hexdigest()


@pytest.mark.asyncio
async def test_v1_audio_promotion_uses_v1_shownotes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fake_generate_audio(_state):
        return {"episode_id": str(uuid.uuid4())}

    async def _unexpected_v2_shownotes(_run_id):
        raise AssertionError("V1 promotion must not build V2 shownotes")

    async def _unexpected_store(_episode_id, _shownotes_json, _quality_metrics):
        raise AssertionError("V1 promotion must not store V2 shownotes")

    async def _fake_build_shownotes(_state):
        calls.append("v1_shownotes")
        return {"sections": [{"title": "V1"}]}

    monkeypatch.setattr("app.generation_v2.finalize.generate_audio", _fake_generate_audio)
    monkeypatch.setattr("app.generation_v2.finalize.build_v2_shownotes_from_artifacts", _unexpected_v2_shownotes)
    monkeypatch.setattr("app.generation_v2.finalize._store_v2_shownotes", _unexpected_store)
    monkeypatch.setattr("app.generation_v2.finalize.build_shownotes", _fake_build_shownotes)

    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=None,
        config={"generation_mode": "v1", "user_prompt": "Topic"},
    )

    await generate_audio_from_segments(run, script_segment_ids=[str(uuid.uuid4())])

    assert calls == ["v1_shownotes"]


@pytest.mark.asyncio
async def test_v2_audio_promotion_uses_v2_shownotes(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict] = []

    async def _fake_generate_audio(_state):
        return {"episode_id": str(uuid.uuid4())}

    async def _fake_v2_shownotes(_run_id):
        return {"sections": [{"title": "V2"}], "v2": True}

    async def _fake_store(_episode_id, shownotes_json, _quality_metrics):
        stored.append(shownotes_json)

    async def _unexpected_build_shownotes(_state):
        raise AssertionError("V2 promotion must use V2 shownotes")

    monkeypatch.setattr("app.generation_v2.finalize.generate_audio", _fake_generate_audio)
    monkeypatch.setattr("app.generation_v2.finalize.build_v2_shownotes_from_artifacts", _fake_v2_shownotes)
    monkeypatch.setattr("app.generation_v2.finalize._store_v2_shownotes", _fake_store)
    monkeypatch.setattr("app.generation_v2.finalize.build_shownotes", _unexpected_build_shownotes)

    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=None,
        config={"generation_mode": "v2", "user_prompt": "Topic"},
    )

    await generate_audio_from_segments(run, script_segment_ids=[str(uuid.uuid4())])

    assert stored == [{"sections": [{"title": "V2"}], "v2": True}]


def test_compare_child_config_is_draft_only_and_variant_scoped() -> None:
    coordinator = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        show_bible_id=uuid.uuid4(),
        config={
            "generation_mode": "compare",
            "execution_mode": "full",
            "duration_target": 3600,
            "user_prompt": "Compare approaches",
        },
        estimated_cost={"estimated_total": 1.23},
    )

    v1_child = _make_compare_child(coordinator, variant="v1")
    v2_child = _make_compare_child(coordinator, variant="v2")

    assert v1_child.config["compare_parent_run_id"] == str(coordinator.id)
    assert v1_child.config["compare_variant"] == "v1"
    assert v1_child.config["compare_draft_only"] is True
    assert v1_child.config["stop_after_node"] == "audit_citations"
    assert v2_child.config["compare_variant"] == "v2"
    assert "stop_after_node" not in v2_child.config


def _source_card(
    *,
    source_id: str | None = None,
    title: str = "Primary source",
    reliability: str = "high",
    authority: str = "primary",
    coverage_status: str = "ready",
    useful_for: list[str] | None = None,
) -> SourceCard:
    return SourceCard(
        source_id=source_id or str(uuid.uuid4()),
        title=title,
        source_type="pdf",
        reliability=reliability,
        authority=authority,
        summary="A concrete source with enough detail for factual claims.",
        useful_for=useful_for or ["mechanism"],
        ready_representation_count=1,
        coverage_status=coverage_status,  # type: ignore[arg-type]
    )


def _claim(
    claim_id: str,
    *,
    support_level: str = "supported",
    source_need: str = "required",
    claim_type: str = "factual",
) -> DraftClaim:
    return DraftClaim(
        claim_id=claim_id,
        statement="A specific factual statement",
        act_number=1,
        line_index=0,
        text_span="A specific factual statement",
        claim_type=claim_type,  # type: ignore[arg-type]
        source_need=source_need,  # type: ignore[arg-type]
        risk_level="medium",
        support_level=support_level,  # type: ignore[arg-type]
    )
