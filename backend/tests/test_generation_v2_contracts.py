from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.generation_v2.agents.editorial import (
    _evidence_inventory,
    _fallback_act_draft,
    _fallback_claims,
    _fallback_research_memo,
    _fallback_treatment,
    audit_research_memo,
    build_act_dossier,
    build_research_memo,
    build_source_triage_agent,
    build_treatment,
    critique_episode,
    extract_and_link_claims,
    judge_claim_support,
    rewrite_act_draft,
    update_continuity_ledger,
    write_act_draft,
)
from app.generation_v2.citation_validation import validate_citations
from app.generation_v2.finalize import generate_audio_from_segments
from app.generation_v2.orchestrator import _validate_research_memo_evidence
from app.generation_v2.prompts import ACT_DRAFT_PROMPT, ACT_REWRITE_PROMPT, CLAIM_CITATION_PROMPT, SOURCE_TRIAGE_PROMPT
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable, _function_calls, _strict_json_schema
from app.generation_v2.schemas import (
    ActDraft,
    CitationLink,
    ClaimCitationBundle,
    ContinuityLedger,
    DialogueLine,
    DraftClaim,
    EditorialAtom,
    EpisodeCritique,
    FactIssue,
    QuoteCard,
    ResearchAudit,
    ResearchMemo,
    SourceCard,
    SourceMap,
    SourceTriageReport,
    SupportJudgment,
    SupportJudgmentBundle,
    Treatment,
    TreatmentAct,
)
from app.generation_v2.source_triage import build_source_triage
from app.generation_v2.source_corpus import _version_has_snapshot, _version_upload_payload
from app.generation_v2.source_workspace import _quote_cards_from_chunks
from app.generation_v2.tools.evidence import (
    _CHUNK_BACKED_VERSION_KINDS,
    _query_tokens,
    _source_map_queries,
    materialize_source_map,
    validate_source_map_gate,
)
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion
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


def test_evidence_tool_descriptions_define_usage_and_citation_contracts() -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    tools = EvidenceToolset(db=None, run=None, run_view=None, provider_index=None).agent_tools()  # type: ignore[arg-type]
    descriptions = {tool.name: tool.description for tool in tools}

    assert "neutral metadata" in descriptions["list_sources"]
    assert "source_ids=[]" in descriptions["search_evidence"]
    assert "persist" in descriptions["search_evidence"].lower()
    assert "source_level or weak evidence is not sufficient" in descriptions["search_evidence"]
    assert "Copy source_id, source_version_id, locator_text, snippet_text" in descriptions["read_evidence"]
    assert "persist that window as an EvidenceItem" in descriptions["read_source"]
    assert "persist representative windows as EvidenceItems" in descriptions["skim_source"]
    assert "not a generic source search tool" in descriptions["find_quotes"]
    assert "corpus/source triage only" in descriptions["search_sources"]
    assert "Compare selected sources" in descriptions["compare_sources"]


def test_prompt_contracts_explain_claim_refs_quote_refs_and_citation_schema() -> None:
    assert "claim_refs are optional temporary fact-check hints only" in ACT_DRAFT_PROMPT
    assert "not claim ids, citation ids, evidence ids, or source ids" in ACT_DRAFT_PROMPT
    assert "You may call search_evidence/read_evidence only to repair a known gap" in ACT_REWRITE_PROMPT
    assert "quote_refs must be evidence_id values or exact source_id:locator refs" in SOURCE_TRIAGE_PROMPT
    assert "Retrieval/read plan" in SOURCE_TRIAGE_PROMPT
    assert "return a complete SourceMap" in SOURCE_TRIAGE_PROMPT
    assert "claim_type=factual" in CLAIM_CITATION_PROMPT
    assert "source_need=required" in CLAIM_CITATION_PROMPT
    assert "risk_level=high" in CLAIM_CITATION_PROMPT
    assert "support_level=supported only when saved evidence directly supports" in CLAIM_CITATION_PROMPT
    assert "If compact evidence is insufficient, call search_evidence" in CLAIM_CITATION_PROMPT
    assert "blocking=true for missing evidence" in CLAIM_CITATION_PROMPT


def test_runtime_tool_trace_names_include_function_tool_names() -> None:
    runtime = V2Runtime()
    tools = runtime._tool_specs(
        vector_store_id=None,
        local_tools=[
            AgentTool(
                name="search_evidence",
                description="Search",
                parameters={"type": "object", "properties": {}},
                handler=None,  # type: ignore[arg-type]
            )
        ],
        policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
    )

    assert tools[0]["name"] == "search_evidence"


@pytest.mark.asyncio
async def test_phase_specific_agent_tool_sets_are_not_broad() -> None:
    _CaptureRuntime.calls = {}
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"},
    )
    all_tools = _all_evidence_tools()
    source = _source_card()
    quote = QuoteCard(
        source_id=source.source_id,
        source_version_id=str(uuid.uuid4()),
        evidence_id=str(uuid.uuid4()),
        quote="A precise saved quote.",
        locator="section 2",
        narrative_use="mechanism",
    )
    treatment_act = TreatmentAct(
        act_number=1,
        title="Mechanism",
        story_role="mechanism",
        opening_question="How does it work?",
        main_turn="The mechanism is concrete.",
        ending_turn="The stakes rise.",
        source_focus=[source.source_id],
        target_minutes=8,
        required_evidence_ids=[quote.evidence_id or ""],
        source_density_target=1,
    )
    treatment = Treatment(
        title="Episode",
        logline="A specific episode.",
        host_dynamic="A asks, B probes.",
        acts=[treatment_act],
    )
    dossier = build_act_dossier(
        treatment_act=treatment_act,
        source_cards=[source],
        quote_cards=[quote],
        ledger=ContinuityLedger(),
    )
    draft = ActDraft(
        act_number=1,
        title="Mechanism",
        lines=[DialogueLine(speaker="A", text="A specific factual statement.")],
        used_source_ids=[source.source_id],
    )
    source_map = SourceMap(source_cards=[source], quote_cards=[quote])
    research_memo = _fallback_research_memo(run, [source], [quote])

    await build_research_memo(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(research_memo),
        source_cards=[source],
        quote_cards=[quote],
        source_map=source_map,
        evidence_tools=all_tools,
    )
    await audit_research_memo(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(ResearchAudit(passed=True, score=0.9, verdict="ok")),
        research_memo=research_memo,
        source_map=source_map,
        evidence_tools=all_tools,
    )
    await build_treatment(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(treatment),
        research_memo=research_memo,
        source_map=source_map,
        evidence_tools=all_tools,
    )
    await write_act_draft(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(draft),
        treatment_act=treatment_act,
        dossier=dossier,
        ledger=ContinuityLedger(),
        evidence_tools=all_tools,
    )
    await critique_episode(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(EpisodeCritique(passed=True, summary="ok")),
        treatment=treatment,
        source_cards=[source],
        quote_cards=[quote],
        drafts=[draft],
        evidence_tools=all_tools,
    )
    await rewrite_act_draft(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(draft),
        treatment_act=treatment_act,
        dossier=dossier,
        draft=draft,
        ledger=ContinuityLedger(),
        critique=EpisodeCritique(passed=False, summary="revise", rewrite_instructions=["tighten"]),
        evidence_tools=all_tools,
    )
    await extract_and_link_claims(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(ClaimCitationBundle(claims=[], citation_links=[])),
        source_cards=[source],
        quote_cards=[quote],
        evidence_records=[],
        evidence_tools=all_tools,
        dossiers=[dossier],
        drafts=[draft],
        critique=EpisodeCritique(passed=True, summary="ok"),
    )

    calls = _CaptureRuntime.calls
    assert set(calls["research_agent"]) == {
        "search_sources",
        "search_evidence",
        "find_quotes",
        "read_evidence",
        "read_source",
        "skim_source",
    }
    assert set(calls["research_auditor"]) == {"search_evidence", "read_evidence"}
    assert set(calls["story_editor"]) == {"search_evidence", "read_evidence"}
    assert set(calls["act_writer_1"]) == {"search_evidence", "read_evidence"}
    assert set(calls["episode_critic"]) == {"search_evidence", "read_evidence"}
    assert set(calls["act_rewriter_1"]) == {"search_evidence", "read_evidence"}
    assert set(calls["citation_linker"]) == {"search_evidence", "read_evidence", "find_quotes"}


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


def test_source_triage_schema_accepts_clusters_relationships_and_top_evidence() -> None:
    report = SourceTriageReport.model_validate(
        {
            "ranked_sources": [],
            "top_sources_by_function": {"mechanism": ["s1"]},
            "top_evidence_by_function": {"mechanism": ["ev1"]},
            "source_clusters": [
                {
                    "cluster_id": "cluster-1",
                    "label": "Primary filings",
                    "source_ids": ["s1", "s2"],
                    "evidence_ids": ["ev1"],
                    "rationale": "Same institution.",
                    "primary_source_ids": ["s1"],
                    "secondary_source_ids": ["s2"],
                }
            ],
            "source_relationships": [
                {
                    "source_id": "s1",
                    "related_source_id": "s2",
                    "relationship": "primary_to_secondary",
                    "evidence_ids": ["ev1"],
                    "rationale": "Article discusses the filing.",
                }
            ],
        }
    )

    assert report.source_clusters[0].primary_source_ids == ["s1"]
    assert report.source_relationships[0].relationship == "primary_to_secondary"
    assert report.top_evidence_by_function["mechanism"] == ["ev1"]


def test_quote_ref_prefers_evidence_id_and_dossier_matches_exact_refs() -> None:
    from app.generation_v2.source_triage import quote_ref

    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    quote = QuoteCard(
        source_id=source_id,
        source_version_id=str(uuid.uuid4()),
        evidence_id=evidence_id,
        quote="A precise saved quote.",
        locator="section 2",
        narrative_use="mechanism",
    )
    act = TreatmentAct(
        act_number=1,
        title="Mechanism",
        story_role="mechanism",
        opening_question="How?",
        main_turn="Mechanism.",
        ending_turn="Next.",
        source_focus=[source_id],
        target_minutes=5,
        required_quote_refs=[evidence_id],
        source_density_target=1,
    )

    dossier = build_act_dossier(
        treatment_act=act,
        source_cards=[_source_card(source_id=source_id)],
        quote_cards=[quote],
        ledger=ContinuityLedger(),
    )

    assert quote_ref(quote) == evidence_id
    assert dossier.quote_cards == [quote]
    assert evidence_id in dossier.evidence_ids


def test_compact_evidence_inventory_caps_and_preserves_preferred_ids() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    preferred_id = str(uuid.uuid4())
    extra_records = [
        _evidence_record(
            evidence_id=str(uuid.uuid4()),
            source_id=source_id,
            source_version_id=source_version_id,
            snippet_text=f"Evidence {idx} " * 100,
        )
        for idx in range(12)
    ]
    preferred_record = _evidence_record(
        evidence_id=preferred_id,
        source_id=source_id,
        source_version_id=source_version_id,
        snippet_text="Preferred evidence " * 100,
    )

    inventory = _evidence_inventory(
        [*extra_records, preferred_record],
        source_cards=[_source_card(source_id=source_id)],
        quote_cards=[
            QuoteCard(
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=preferred_id,
                quote="Preferred evidence.",
                locator="section 2",
                narrative_use="mechanism",
            )
        ],
        limit=5,
    )

    assert len(inventory) == 5
    assert inventory[0]["id"] == preferred_id
    assert len(inventory[0]["snippet_text"]) <= 900


def test_materialize_source_map_adds_new_triage_evidence_and_quote_cards() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            _source_card(source_id=source_id).model_copy(
                update={"source_version_ids": [source_version_id], "evidence_ids": []}
            )
        ]
    )
    source_map = SourceMap(
        source_cards=[],
        quote_cards=[],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [evidence_id]}),
        evidence_ids=[],
    )

    materialized = materialize_source_map(
        source_map,
        inventory=inventory,
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
                snippet_text="A saved source window explains the mechanism clearly.",
            )
        ],
    )

    assert materialized.evidence_ids == [evidence_id]
    assert materialized.source_cards[0].evidence_ids == [evidence_id]
    assert materialized.quote_cards[0].evidence_id == evidence_id
    assert materialized.retrieval_notes


def test_research_memo_evidence_gate_blocks_unanchored_high_impact_atoms() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        narrative_spine="Spine",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A specific mechanism.",
                source_ids=[source_id],
                evidence_ids=[],
                story_value="Explains the system.",
            )
        ],
    )
    source_map = SourceMap(
        source_cards=[_source_card(source_id=source_id).model_copy(update={"evidence_ids": [evidence_id]})],
        evidence_ids=[evidence_id],
    )

    gate = _validate_research_memo_evidence(memo, source_map, 3600)

    assert gate.passed is False
    assert "High-impact atom 1" in gate.missing_material[0]


@pytest.mark.asyncio
async def test_source_triage_gate_rejects_missing_tool_calls_for_non_tiny_corpus() -> None:
    source_id = uuid.uuid4()
    other_source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={"source_version_ids": [str(source_version_id)], "evidence_ids": [str(evidence_id)]}
            )
        ],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(evidence_id)]}),
        evidence_ids=[str(evidence_id)],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(id=evidence_id, source_id=source_id, source_version_id=source_version_id),
            ],
            traces=[],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id), str(other_source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is False
    assert gate.tool_call_count == 0
    assert "No triage reading/search tool calls" in gate.summary


@pytest.mark.asyncio
async def test_source_triage_gate_rejects_hallucinated_evidence_ids() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    valid_evidence_id = uuid.uuid4()
    hallucinated_evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(valid_evidence_id), str(hallucinated_evidence_id)],
                }
            )
        ],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(hallucinated_evidence_id)]}),
        evidence_ids=[str(valid_evidence_id), str(hallucinated_evidence_id)],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(id=valid_evidence_id, source_id=source_id, source_version_id=source_version_id),
            ],
            traces=[SimpleNamespace(tool_name="skim_source")],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id), str(uuid.uuid4())],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is False
    assert gate.invalid_evidence_ids == [str(hallucinated_evidence_id)]
    assert gate.missing_function_evidence == ["mechanism"]


@pytest.mark.asyncio
async def test_source_triage_agent_uses_full_reading_tool_set() -> None:
    _CaptureRuntime.calls = {}
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})
    source_map = SourceMap(source_cards=[_source_card()], quote_cards=[], gaps=["Need primary sources"])

    result = await build_source_triage_agent(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(source_map),
        source_inventory=SourceMap(source_cards=[_source_card()], quote_cards=[]),
        evidence_tools=_all_evidence_tools(),
    )

    assert result.gaps == ["Need primary sources"]
    assert set(_CaptureRuntime.calls["source_triage_agent"]) == {
        "list_sources",
        "search_sources",
        "search_evidence",
        "find_quotes",
        "read_evidence",
        "read_source",
        "skim_source",
        "compare_sources",
    }


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


@pytest.mark.asyncio
async def test_evidence_backed_citation_validation_passes_with_saved_evidence() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    quote = "The audit found that the mechanism depends on three concrete constraints."
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section: section 2",
                quote=quote,
                supports_span="A specific factual statement",
                confidence=0.92,
            )
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
                snippet_text=f"{quote} The rest of the snippet explains the result.",
                evidence_strength="snippet",
            )
        ],
        support_judgments=[
            SupportJudgment(
                claim_id="claim-1",
                evidence_id=evidence_id,
                supported=True,
                support_level="supported",
                rationale="The linked snippet directly supports the claim.",
                required_repair="",
                blocking=False,
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is True
    assert report.quote_match_count == 1
    assert report.blocking_issue_count == 0


@pytest.mark.asyncio
async def test_evidence_gate_blocks_missing_evidence_and_source_level_support() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    bundle = ClaimCitationBundle(
        claims=[
            _claim("missing-evidence", support_level="supported"),
            _claim("weak-evidence", support_level="supported"),
        ],
        citation_links=[
            CitationLink(
                claim_id="missing-evidence",
                source_id=source_id,
                source_version_id=source_version_id,
                locator="section 2",
                confidence=0.9,
            ),
            CitationLink(
                claim_id="weak-evidence",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="source snapshot",
                confidence=0.9,
            ),
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_strength="source_level",
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    issue_text = " ".join(issue.issue for issue in report.issues)
    assert report.passed is False
    assert "missing evidence_id" in issue_text
    assert "weak or source-level evidence" in issue_text


@pytest.mark.asyncio
async def test_required_evidence_link_requires_support_judgment() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section 2",
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
            )
        ],
        support_judgments=[],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is False
    assert any("missing a saved support judgment" in issue.issue for issue in report.issues)


@pytest.mark.asyncio
async def test_supported_judgment_overrides_linker_self_reported_weak_support() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="weak")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section 2",
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
            )
        ],
        support_judgments=[
            SupportJudgment(
                claim_id="claim-1",
                evidence_id=evidence_id,
                supported=True,
                support_level="supported",
                rationale="Saved evidence supports the claim.",
                blocking=False,
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is True
    assert report.supported_required_claim_count == 1
    assert not any("self-reported support" in issue.issue for issue in report.issues)


@pytest.mark.asyncio
async def test_support_judgment_blocks_unsupported_saved_evidence() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section 2",
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
            )
        ],
        support_judgments=[
            SupportJudgment(
                claim_id="claim-1",
                evidence_id=evidence_id,
                supported=False,
                support_level="unsupported",
                rationale="Snippet discusses a different point.",
                required_repair="Relink to stronger evidence.",
                blocking=True,
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is False
    assert report.unsupported_judgment_count == 1
    assert any("Support judge marked evidence as not supported" in issue.issue for issue in report.issues)


@pytest.mark.asyncio
async def test_support_judgment_supported_false_blocks_even_with_supported_level() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    source = _source_card(source_id=source_id)
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section 2",
                confidence=0.9,
            )
        ],
    )

    report = await validate_citations(
        None,
        bundle=bundle,
        source_cards=[source],
        quote_cards=[],
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
            )
        ],
        support_judgments=[
            SupportJudgment(
                claim_id="claim-1",
                evidence_id=evidence_id,
                supported=False,
                support_level="supported",
                rationale="Contradictory verifier payload.",
                required_repair="Relink to stronger evidence.",
                blocking=False,
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is False
    assert report.unsupported_judgment_count == 1
    assert any("marked evidence as not supported" in issue.issue for issue in report.issues)


@pytest.mark.asyncio
async def test_support_judge_cannot_call_evidence_tools() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    bundle = ClaimCitationBundle(
        claims=[_claim("claim-1", support_level="supported")],
        citation_links=[
            CitationLink(
                claim_id="claim-1",
                source_id=source_id,
                source_version_id=source_version_id,
                evidence_id=evidence_id,
                locator="section 2",
                confidence=0.9,
            )
        ],
    )
    runtime = _FakeSupportJudgeRuntime(evidence_id=evidence_id)
    dummy_tool = AgentTool(
        name="search_evidence",
        description="Should not be exposed to support judge",
        parameters={"type": "object", "properties": {}},
        handler=None,  # type: ignore[arg-type]
    )

    await judge_claim_support(
        None,  # type: ignore[arg-type]
        SimpleNamespace(id=uuid.uuid4()),
        runtime,  # type: ignore[arg-type]
        bundle=bundle,
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
            )
        ],
        evidence_tools=[dummy_tool],
    )

    assert runtime.local_tools == []
    assert runtime.tool_policy.local_tools is False
    assert runtime.tool_policy.web_search is False


def test_unicode_query_tokens_include_cyrillic_terms() -> None:
    assert {"корпус", "модель"} <= set(_query_tokens("Корпус большой, модель ищет источники"))


def test_source_map_queries_sample_editorial_functions() -> None:
    labels = [label for label, _query in _source_map_queries("AI regulation")]

    assert {"overview", "conflict", "mechanism", "history", "consequence", "human_cost", "exception"} <= set(labels)


def test_url_and_original_file_versions_are_not_local_snapshots() -> None:
    base = {
        "id": uuid.uuid4(),
        "source_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "content_hash": "hash",
        "metadata_": {},
        "status": "ready",
        "upload_status": "pending",
        "is_active": True,
    }
    url_version = SourceVersion(kind=SourceRepresentationKind.ORIGINAL_URL, storage_path="https://example.com", **base)
    file_version = SourceVersion(kind=SourceRepresentationKind.ORIGINAL_FILE, storage_path="sources/project/report.pdf", **base)
    html_version = SourceVersion(kind=SourceRepresentationKind.FETCHED_HTML, storage_path="snapshots/fetched.html", **base)

    assert _version_has_snapshot(url_version) is False
    assert _version_has_snapshot(file_version) is False
    assert _version_has_snapshot(html_version) is True


def test_chunk_backed_version_kinds_exclude_snapshots_and_originals() -> None:
    assert SourceRepresentationKind.EXTRACTED_TEXT in _CHUNK_BACKED_VERSION_KINDS
    assert SourceRepresentationKind.TRANSCRIPT in _CHUNK_BACKED_VERSION_KINDS
    assert SourceRepresentationKind.ORIGINAL_FILE not in _CHUNK_BACKED_VERSION_KINDS
    assert SourceRepresentationKind.FETCHED_HTML not in _CHUNK_BACKED_VERSION_KINDS
    assert SourceRepresentationKind.PAGE_MARKED_TEXT not in _CHUNK_BACKED_VERSION_KINDS


def test_original_file_provider_upload_payload_preserves_native_bytes(monkeypatch) -> None:
    payload = b"%PDF-1.7\nbinary-ish bytes\x00\xff"

    def fake_download_file(_client, path):
        assert path == "sources/project/report.pdf"
        return payload

    monkeypatch.setattr("app.generation_v2.source_corpus.download_file", fake_download_file)
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/project/report.pdf",
        content_hash=hashlib.sha256(payload).hexdigest(),
        metadata_={"original_filename": "report.pdf", "source_type": "pdf"},
        status="ready",
        upload_status="pending",
        is_active=True,
    )

    filename, content = _version_upload_payload(version)

    assert filename == "report.pdf"
    assert content == payload


def test_act_dossier_preserves_required_evidence_ids() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    quote = QuoteCard(
        source_id=source_id,
        source_version_id=str(uuid.uuid4()),
        evidence_id=evidence_id,
        quote="A precise saved quote.",
        locator="section 2",
        narrative_use="mechanism",
    )
    act = TreatmentAct(
        act_number=1,
        title="Mechanism",
        story_role="mechanism",
        opening_question="How does this work?",
        main_turn="The mechanism is specific.",
        ending_turn="The next act raises the stakes.",
        source_focus=[source_id],
        target_minutes=10,
        required_evidence_ids=[evidence_id],
        source_density_target=1,
    )

    dossier = build_act_dossier(
        treatment_act=act,
        source_cards=[_source_card(source_id=source_id)],
        quote_cards=[quote],
        ledger=ContinuityLedger(),
    )

    assert dossier.evidence_ids == [evidence_id]
    assert evidence_id in dossier.anchor_refs


class _FakeSupportJudgeRuntime:
    model = "fake-support-judge"

    def __init__(self, *, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        self.local_tools = None
        self.tool_policy = None

    async def call_structured(self, **kwargs):
        self.local_tools = kwargs["local_tools"]
        self.tool_policy = kwargs["tool_policy"]
        schema_model = kwargs["schema_model"]
        assert schema_model is SupportJudgmentBundle
        return schema_model.model_validate(
            {
                "judgments": [
                    {
                        "claim_id": "claim-1",
                        "evidence_id": self.evidence_id,
                        "supported": True,
                        "support_level": "supported",
                        "rationale": "Saved evidence supports the claim.",
                        "required_repair": "",
                        "blocking": False,
                    }
                ]
            }
        )


class _CaptureRuntime:
    model = "fake-capture"
    calls: dict[str, list[str]] = {}

    def __init__(self, response):
        self.response = response

    async def call_structured(self, **kwargs):
        self.calls[kwargs["agent_name"]] = [tool.name for tool in kwargs.get("local_tools") or []]
        return self.response

    def prompt_hash_for(self, _phase: str, _agent_name: str) -> str | None:
        return None

    def fallback_metadata_for(self, _phase: str, _agent_name: str) -> dict | None:
        return None


class _FakeGateDb:
    def __init__(self, *, evidence: list[SimpleNamespace], traces: list[SimpleNamespace]) -> None:
        self._responses = [_FakeScalarResult(evidence), _FakeScalarResult(traces)]

    async def execute(self, _query):
        return self._responses.pop(0)


class _FakeScalarResult:
    def __init__(self, values: list[SimpleNamespace]) -> None:
        self._values = values

    def scalars(self):
        return self._values


def _all_evidence_tools() -> list[AgentTool]:
    async def _handler(_args):
        return {}

    return [
        AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=_handler,
        )
        for name in (
            "list_sources",
            "search_sources",
            "search_evidence",
            "find_quotes",
            "read_evidence",
            "read_source",
            "skim_source",
            "compare_sources",
        )
    ]


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


def _evidence_record(
    *,
    evidence_id: str,
    source_id: str,
    source_version_id: str,
    snippet_text: str = "The audit found that the mechanism depends on three concrete constraints.",
    evidence_strength: str = "snippet",
) -> dict:
    return {
        "id": evidence_id,
        "generation_run_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "source_id": source_id,
        "source_version_id": source_version_id,
        "provider": "openai",
        "provider_vector_store_id": "vs_test",
        "provider_file_id": "file_test",
        "provider_vector_store_file_id": "vsf_test",
        "retrieval_channel": "provider_vector_search",
        "query_text": "mechanism",
        "purpose": "citation_linking",
        "search_result_index": 0,
        "score": 0.9,
        "snippet_text": snippet_text,
        "normalized_quote_text": snippet_text.lower(),
        "locator_text": "section 2",
        "locator_type": "section",
        "evidence_strength": evidence_strength,
        "snippet_hash": hashlib.sha256(snippet_text.encode("utf-8")).hexdigest(),
        "metadata": {},
    }


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
