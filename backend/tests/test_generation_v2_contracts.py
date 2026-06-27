from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.config import settings
from app.generation_v2.agents.editorial import (
    _evidence_inventory,
    _fallback_act_draft,
    _fallback_claims,
    _fallback_research_memo,
    _fallback_treatment,
    _quote_inventory,
    _source_inventory,
    _source_ref_context,
    _triage_inventory,
    audit_research_memo,
    build_act_dossier,
    build_research_memo,
    build_source_acquisition_agent,
    build_source_triage_agent,
    build_source_triage_finalizer_agent,
    build_source_triage_planner_agent,
    build_source_triage_set_agent,
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
from app.generation_v2.orchestrator import (
    _research_memo_evidence_ids,
    _save_evidence_artifact_map,
    _source_triage_repair_context,
    _validate_research_memo_evidence,
)
from app.generation_v2.prompting import (
    ACT_DRAFT_PROMPT,
    ACT_REWRITER_SYSTEM,
    ACT_REWRITE_PROMPT,
    ACT_WRITER_SYSTEM,
    CLAIM_CITATION_PROMPT,
    CITATION_LINKER_SYSTEM,
    EPISODE_CRITIQUE_PROMPT,
    EPISODE_CRITIC_SYSTEM,
    RESEARCH_AUDIT_SYSTEM,
    RESEARCH_FINALIZER_PROMPT,
    RESEARCH_JOB_PROMPT,
    RESEARCH_PLANNER_PROMPT,
    RESEARCH_SYSTEM,
    SOURCE_ACQUISITION_PROMPT,
    SOURCE_ACQUISITION_SYSTEM,
    SOURCE_TRIAGE_FINALIZER_PROMPT,
    SOURCE_TRIAGE_PLANNER_PROMPT,
    SOURCE_TRIAGE_PROMPT,
    SOURCE_TRIAGE_REPAIR_PROMPT,
    SOURCE_TRIAGE_SET_PROMPT,
    SOURCE_TRIAGE_SYSTEM,
    SUPPORT_JUDGE_SYSTEM,
    TREATMENT_SYSTEM,
)
from app.generation_v2.runtime import (
    AgentCheckpointPolicy,
    AgentTool,
    ToolPolicy,
    ToolOutputContractViolation,
    V2Runtime,
    V2RuntimeUnavailable,
    _file_search_calls,
    _function_calls,
    _hosted_evidence_followup_input,
    _model_facing_tool_output,
    _strict_json_schema,
)
from app.generation_v2.research_workspace import (
    build_research_workspace_tools,
    research_finalizer_payload_for_prompt,
    research_job_prompt_payload,
    research_memo_from_workspace,
    source_map_research_summary,
)
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
    CorpusGapReport,
    SourceAcquisitionCandidate,
    SourceCard,
    SourceCluster,
    SourceMap,
    SourceRelationship,
    SourceTriageGate,
    SourceTriageCompletion,
    SourceTriageItem,
    SourceTriageReport,
    SupportJudgment,
    SupportJudgmentBundle,
    Treatment,
    TreatmentAct,
)
from app.generation_v2.source_triage import build_source_triage
from app.generation_v2.source_acquisition import (
    SourceAcquisitionToolset,
    build_corpus_gap_report,
    normalize_candidate_url,
    validate_candidate_static,
)
from app.generation_v2.source_corpus import (
    _agent_facing_provider_versions,
    _call_openai_url_file_read,
    _canonical_provider_version,
    _ensure_provider_file,
    _ensure_source_version,
    _filename_from_url,
    _is_pdf_response,
    _provider_attributes,
    _version_has_snapshot,
    _version_upload_payload,
    prepare_v2_corpus,
    reconcile_project_corpus_index_now,
    reconcile_provider_corpus_files,
)
from app.generation_v2.source_workspace import _quote_cards_from_chunks
from app.generation_v2 import source_triage_workspace as triage_workspace_module
from app.generation_v2.source_triage_workspace import (
    create_repair_sets_from_gate,
    build_workspace_tools,
    unresolved_source_sets,
)
from app.generation_v2.tools.evidence import (
    _CHUNK_BACKED_VERSION_KINDS,
    _query_tokens,
    build_source_map_from_write_actions,
    validate_source_map_gate,
    EvidenceToolset,
)
from app.generation_v2.tools.evidence_utils import _provider_filters
from app.services.llm import _openai_estimated_cost_usd, _openai_token_cost_payload, _openai_usage
from app.ingestion.source_metadata import audited_authority_type, classify_authority_heuristic
from app.models.generation_v2 import (
    EvidenceRetrievalChannel,
    EvidenceStrength,
    EvidenceItem,
    ProviderCorpusFile,
    ProviderCorpusIndex,
    RunCorpusView,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
    V2ToolTrace,
)
from app.models.source import AuthorityType, Source, SourceType
from app.schemas.generation import GenerationCreate
from app.workers.generation_tasks import _make_compare_child


def test_generation_create_defaults_to_v1_full_mode() -> None:
    payload = GenerationCreate(duration_target=1800, user_prompt="Explain a topic")

    assert payload.generation_mode == "v1"
    assert payload.execution_mode == "full"


def test_v2_native_ready_status_survives_deterministic_parser_failure() -> None:
    from app.workers.ingestion_tasks import _v2_native_source_ready

    assert _v2_native_source_ready({"v2_native_ingest": {"status": "provider_ready"}}) is True
    assert _v2_native_source_ready({"v2_native_ingest": {"ready_provider_file_count": "2"}}) is True
    assert _v2_native_source_ready({"v2_native_ingest": {"openai_url_read_status": "readable"}}) is True
    assert _v2_native_source_ready({"v2_native_ingest": {"status": "source_versions_ready"}}) is False
    assert _v2_native_source_ready({"v2_native_ingest": {"status": "failed", "ready_provider_file_count": "2"}}) is False


def test_authority_heuristic_treats_academic_pdf_as_secondary_not_primary() -> None:
    authority = classify_authority_heuristic(
        source_type=SourceType.PDF,
        original_url="https://www.diva-portal.org/smash/get/diva2:1704478/FULLTEXT01.pdf",
        title="FULLTEXT01",
        text_preview="One Country, Two Currencies. Research paper on Hong Kong monetary history.",
    )

    assert authority == AuthorityType.SECONDARY


def test_generation_inventory_audits_legacy_primary_academic_sources_to_secondary() -> None:
    source = SimpleNamespace(
        source_type=SourceType.PDF,
        original_url="https://uu.diva-portal.org/smash/get/diva2:1704478/FULLTEXT01.pdf",
        title="FULLTEXT01",
        text_preview="Peer-reviewed journal article about Hong Kong monetary history.",
        authority_type=AuthorityType.PRIMARY,
    )

    assert audited_authority_type(source) == AuthorityType.SECONDARY


def test_authority_heuristic_keeps_official_regulator_page_primary() -> None:
    authority = classify_authority_heuristic(
        source_type=SourceType.WEB,
        original_url="https://www.hkma.gov.hk/eng/key-functions/money/",
        title="Hong Kong Monetary Authority - How Does the LERS Work?",
        text_preview="Official HKMA explanation of the Linked Exchange Rate System.",
    )

    assert authority == AuthorityType.PRIMARY


def test_strict_json_schema_requires_nested_properties_and_blocks_extra_fields() -> None:
    schema = _strict_json_schema(ResearchMemo.model_json_schema())

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"title", "thesis", "research_synthesis", "editorial_atoms"}
    assert "narrative_spine" not in schema.get("properties", {})
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


def test_file_search_call_parser_extracts_hosted_results() -> None:
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="file_search_call",
                id="fs-call-1",
                queries=["mechanism"],
                status="completed",
                results=[
                    SimpleNamespace(
                        file_id="file_123",
                        filename="report.pdf",
                        score=0.91,
                        text="The relevant source snippet.",
                    )
                ],
            )
        ]
    )

    calls = _file_search_calls(response)

    assert calls[0]["call_id"] == "fs-call-1"
    assert calls[0]["queries"] == ["mechanism"]
    assert calls[0]["results"][0]["file_id"] == "file_123"
    assert calls[0]["results"][0]["result_index"] == 0


def test_hosted_file_search_followup_exposes_observed_evidence_ids() -> None:
    async def _handler(_args):
        return {}

    followup = _hosted_evidence_followup_input(
        {
            "observed_evidence": [
                {
                    "id": "ev-1",
                    "source_ref": "S01",
                    "source_title": "Currency note issuer history",
                    "source_domain": "hkma.gov.hk",
                    "locator_text": "page 2",
                    "score": 0.8,
                    "snippet_text": "A hosted result snippet.",
                }
            ]
        },
        announced_ids=set(),
        tools=[AgentTool(name="select_evidence", description="", parameters={}, handler=_handler)],
    )

    assert followup is not None
    assert followup["evidence_ids"] == ["ev-1"]
    assert "ev-1" in followup["message"]["content"]
    assert "S01" in followup["message"]["content"]
    assert "Currency note issuer history" in followup["message"]["content"]
    assert "select_evidence" in followup["message"]["content"]
    assert "discovery previews" in followup["message"]["content"]
    assert "retrieval_channel" not in followup["message"]["content"]
    assert "source_version_id" not in followup["message"]["content"]


def test_hosted_file_search_tool_filters_to_agent_facing_files() -> None:
    runtime = V2Runtime()

    tools = runtime._tool_specs(
        vector_store_id="vs_123",
        local_tools=[],
        policy=ToolPolicy(file_search=True, web_search=False, local_tools=False),
    )

    file_search_tool = tools[0]
    assert file_search_tool["type"] == "file_search"
    assert file_search_tool["filters"] == {
        "type": "and",
        "filters": [
            {"type": "eq", "key": "is_active", "value": True},
            {"type": "eq", "key": "agent_facing", "value": True},
            {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
        ],
    }


def test_runtime_model_facing_tool_output_validates_compact_contracts() -> None:
    output = {
        "results": [
                {
                    "evidence_id": "ev-1",
                    "source_ref": "S01",
                    "source_title": "Currency note issuer history",
                    "source_domain": "hkma.gov.hk",
                    "locator": "section 1",
                    "short_snippet": "compact preview",
                    "score": 0.9,
            }
        ],
        "cache_hit": False,
    }

    model_output = _model_facing_tool_output("search_corpus", output)

    assert model_output == output
    assert "_model_output_truncated" not in json.dumps(model_output, ensure_ascii=False)

    with pytest.raises(ToolOutputContractViolation, match="forbidden model-facing keys"):
        _model_facing_tool_output(
            "search_corpus",
            {"results": [{"evidence_id": "ev-1", "source_id": "src-1", "source_version_id": "ver-1"}]},
        )


def test_runtime_model_facing_tool_output_rejects_unsafe_or_oversized_payloads() -> None:
    with pytest.raises(ToolOutputContractViolation, match="forbidden model-facing keys"):
        _model_facing_tool_output("search_corpus", {"provider_file_id": "file_secret", "results": []})

    with pytest.raises(ToolOutputContractViolation, match="exceeded"):
        _model_facing_tool_output("search_corpus", {"results": [{"short_snippet": "x" * 13000}]})


class _RuntimeOk(BaseModel):
    ok: bool


def test_v2_runtime_routes_models_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "generation_v2_openai_model", "legacy-gpt-5")
    monkeypatch.setattr(settings, "generation_v2_default_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_source_triage_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_source_acquisition_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_research_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_research_planner_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_research_job_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_research_finalizer_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_treatment_model", "gpt-5.4")
    monkeypatch.setattr(settings, "generation_v2_writer_model", "gpt-5.4")
    monkeypatch.setattr(settings, "generation_v2_rewriter_model", "gpt-5.4")
    monkeypatch.setattr(settings, "generation_v2_citation_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_support_judge_model", "gpt-5.4-mini")

    runtime = V2Runtime()

    assert runtime.model_for("v2_source_triage", "source_triage_agent") == "gpt-5.4-mini"
    assert runtime.model_for("v2_source_acquisition", "source_acquisition_round_2") == "gpt-5.4-mini"
    assert runtime.model_for("v2_editorial_research", "research_planner_agent") == "gpt-5.4-mini"
    assert runtime.model_for("v2_editorial_research", "research_job_agent_mechanism") == "gpt-5.4-mini"
    assert runtime.model_for("v2_editorial_research", "research_finalizer_agent") == "gpt-5.4-mini"
    assert runtime.model_for("v2_treatment", "story_editor") == "gpt-5.4"
    assert runtime.model_for("v2_act_drafting", "act_writer_2") == "gpt-5.4"
    assert runtime.model_for("v2_critique_rewrite", "act_rewriter_2") == "gpt-5.4"
    assert runtime.model_for("v2_fact_citation", "citation_linker") == "gpt-5.4-mini"
    assert runtime.model_for("v2_fact_citation", "support_judge") == "gpt-5.4-mini"
    assert runtime.model_for("v2_unknown", "unknown_agent") == "gpt-5.4-mini"


@pytest.mark.asyncio
async def test_runtime_checkpoint_restarts_chain_after_input_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import runtime as runtime_module

    class _FakeResponses:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(
                    id="resp_1",
                    status="completed",
                    output_text="",
                    output=[
                        {
                            "type": "function_call",
                            "name": "lookup",
                            "call_id": "call_1",
                            "arguments": "{}",
                        }
                    ],
                    usage=SimpleNamespace(input_tokens=70_000, output_tokens=10, total_tokens=70_010),
                )
            return SimpleNamespace(
                id="resp_2",
                status="completed",
                output_text='{"ok":true}',
                output=[],
                usage=SimpleNamespace(input_tokens=1200, output_tokens=8, total_tokens=1208),
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.responses = _FakeResponses()

    class _FakeResult:
        def scalars(self):
            return []

        def scalar_one_or_none(self):
            return None

        def all(self):
            return []

    class _FakeDb:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def execute(self, _query):
            return _FakeResult()

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            for item in self.added:
                if isinstance(item, V2ToolTrace) and item.id is None:
                    item.id = uuid.uuid4()

    fake_client = _FakeClient()
    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_default_model", "gpt-5.4-mini")
    monkeypatch.setattr(settings, "generation_v2_source_triage_model", "gpt-5.4-mini")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: fake_client)

    async def lookup(_args: dict) -> dict:
        return {"accepted": True, "result": "small result"}

    result = await V2Runtime().call_structured(
        db=fake_db,  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        phase="v2_source_triage",
        agent_name="source_triage_agent",
        schema_model=_RuntimeOk,
        system="system",
        prompt="Run triage for a topic with the available source tools.",
        local_tools=[
            AgentTool(
                name="lookup",
                description="Lookup",
                parameters={"type": "object", "properties": {}},
                handler=lookup,
            )
        ],
        tool_policy=ToolPolicy(file_search=False, local_tools=True),
        checkpoint_policy=AgentCheckpointPolicy(enabled=True, max_chain_input_tokens=60_000, max_chain_rounds=99),
        max_tool_rounds=3,
    )

    assert result.ok is True
    assert len(fake_client.responses.calls) == 2
    assert fake_client.responses.calls[0]["model"] == "gpt-5.4-mini"
    assert fake_client.responses.calls[1]["model"] == "gpt-5.4-mini"
    assert fake_client.responses.calls[0]["previous_response_id"] is runtime_module.openai.NOT_GIVEN
    assert fake_client.responses.calls[1]["previous_response_id"] is runtime_module.openai.NOT_GIVEN
    assert "Responses chain checkpoint restart" in fake_client.responses.calls[1]["input"][0]["content"]
    assert "Source triage workflow" in fake_client.responses.calls[1]["input"][0]["content"]
    assert not any(item.get("type") == "function_call_output" for item in fake_client.responses.calls[1]["input"])
    checkpoint_traces = [item for item in fake_db.added if isinstance(item, V2ToolTrace) and item.tool_name == "runtime.checkpoint_restart"]
    assert checkpoint_traces
    tool_traces = [item for item in fake_db.added if isinstance(item, V2ToolTrace) and item.tool_name == "lookup"]
    assert tool_traces[0].output_json["_runtime"]["model_facing_tool_output_chars"] > 0
    assert tool_traces[0].output_json["_runtime"]["model_facing_schema"] == "unknown_tool_output"
    assert "model_facing_tool_output_truncated" not in tool_traces[0].output_json["_runtime"]
    response_traces = [item for item in fake_db.added if isinstance(item, V2ToolTrace) and item.tool_name == "openai.responses.create"]
    assert response_traces[0].input_json["requested_model"] == "gpt-5.4-mini"
    assert response_traces[0].output_json["requested_model"] == "gpt-5.4-mini"
    assert response_traces[0].output_json["billing_model"] == "gpt-5.4-mini"
    assert response_traces[0].output_json["estimated_token_cost_usd"] is not None
    assert response_traces[0].output_json["cost_basis"] == "openai_token_usage_only"
    assert "estimated_cost_usd" not in response_traces[0].output_json


@pytest.mark.asyncio
async def test_runtime_contract_violation_records_trace_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import runtime as runtime_module

    class _FakeResponses:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                id="resp_1",
                status="completed",
                output_text="",
                output=[
                    {
                        "type": "function_call",
                        "name": "lookup",
                        "call_id": "call_1",
                        "arguments": "{}",
                    }
                ],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=10, total_tokens=1010),
            )

    class _FakeClient:
        def __init__(self) -> None:
            self.responses = _FakeResponses()

    class _FakeDb:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            return None

    async def lookup(_args: dict) -> dict:
        return {"results": [{"short_snippet": "x" * 13000}]}

    fake_db = _FakeDb()
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(runtime_module, "get_openai_client", lambda: _FakeClient())

    with pytest.raises(ToolOutputContractViolation, match="exceeded"):
        await V2Runtime().call_structured(
            db=fake_db,  # type: ignore[arg-type]
            generation_run_id=uuid.uuid4(),
            phase="v2_source_triage",
            agent_name="source_triage_agent",
            schema_model=_RuntimeOk,
            system="system",
            prompt="prompt",
            local_tools=[
                AgentTool(
                    name="lookup",
                    description="Lookup",
                    parameters={"type": "object", "properties": {}},
                    handler=lookup,
                )
            ],
            tool_policy=ToolPolicy(file_search=False, local_tools=True),
            checkpoint_policy=AgentCheckpointPolicy(enabled=False),
            max_tool_rounds=1,
        )

    trace = next(item for item in fake_db.added if isinstance(item, V2ToolTrace) and item.tool_name == "lookup")
    assert "contract_violation" in trace.output_json["_runtime"]
    assert "model_facing_tool_output_truncated" not in trace.output_json["_runtime"]


def test_openai_usage_parser_captures_cached_tokens_and_cost() -> None:
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            total_tokens=1100,
            input_tokens_details=SimpleNamespace(cached_tokens=400),
            output_tokens_details=SimpleNamespace(reasoning_tokens=25),
        )
    )

    usage = _openai_usage(response)

    assert usage["cached_input_tokens"] == 400
    assert usage["reasoning_output_tokens"] == 25
    assert _openai_estimated_cost_usd("gpt-5.4-mini", usage) == 0.00093
    assert _openai_estimated_cost_usd("gpt-5.4", usage) == 0.0031
    cost_payload = _openai_token_cost_payload("gpt-5.4-mini", usage)
    assert cost_payload["estimated_token_cost_usd"] == 0.00093
    assert cost_payload["cost_basis"] == "openai_token_usage_only"
    assert "not an OpenAI invoice total" in cost_payload["cost_warning"]


def test_tool_round_limit_zero_disables_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.runtime import _tool_round_limit

    monkeypatch.setattr(settings, "generation_v2_max_tool_rounds", 8)

    assert _tool_round_limit(0) is None
    assert _tool_round_limit(-1) is None
    assert _tool_round_limit(96) == 96
    assert _tool_round_limit(None) == 8


def test_provider_search_filters_to_agent_facing_files() -> None:
    assert _provider_filters([]) == {
        "type": "and",
        "filters": [
            {"type": "eq", "key": "is_active", "value": True},
            {"type": "eq", "key": "agent_facing", "value": True},
            {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
        ],
    }


@pytest.mark.asyncio
async def test_search_corpus_cache_hit_skips_provider_and_local_search() -> None:
    from app.generation_v2.tools import evidence_retrieval as evidence_module

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    run_view = SimpleNamespace(
        selected_source_ids=[str(source_id)],
        selected_source_version_ids=[str(source_version_id)],
        manifest_hash="manifest-1",
    )
    cache_key = evidence_module._search_cache_key(
        run_view,
        query="currency board mechanism",
        source_ids=[str(source_id)],
        limit=1,
    )
    cached = EvidenceItem(
        id=uuid.uuid4(),
        generation_run_id=run_id,
        project_id=project_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider=None,
        retrieval_channel="local_chunk_search",
        query_text="currency board mechanism",
        purpose="source_map",
        search_result_index=0,
        score=0.9,
        snippet_text="Cached evidence about the currency board mechanism.",
        normalized_quote_text="cached evidence",
        locator_text="section 1",
        locator_type="section",
        evidence_strength="exact",
        snippet_hash=hashlib.sha256(b"cached").hexdigest(),
        metadata_={"usage_status": "observed", "search_cache_key": cache_key},
    )

    class _FakeDb:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def execute(self, _query):
            return _FakeScalarResult([cached])

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            return None

    toolset = EvidenceToolset(
        db=_FakeDb(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=run_view,
        provider_index=None,
    )

    async def fail_search(**_kwargs):
        raise AssertionError("search should use cache")

    toolset._provider_search = fail_search  # type: ignore[method-assign]
    toolset._local_fallback_search = fail_search  # type: ignore[method-assign]

    result = await toolset.search_corpus(
        query="currency board mechanism",
        purpose="source_map",
        source_ids=[str(source_id)],
        limit=1,
    )

    assert result["cache_hit"] is True
    assert result["results"][0]["evidence_id"] == str(cached.id)
    assert result["results"][0]["short_snippet"] == "Cached evidence about the currency board mechanism."


@pytest.mark.asyncio
async def test_read_source_with_model_creates_evidence_item(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_retrieval as evidence_module

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    provider_index_id = uuid.uuid4()
    provider_index = ProviderCorpusIndex(
        id=provider_index_id,
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    version = SourceVersion(
        id=source_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/report.pdf",
        content_hash="hash",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider="openai",
        provider_file_id="file_test",
        provider_vector_store_file_id="vsf_test",
        attributes={"filename": "report.pdf"},
        status=V2RecordStatus.READY,
    )
    fake_client = _FakeModelReadClient(
        {
            "supports_query": True,
            "support_level": "direct",
            "quote_text": "The report directly explains the mechanism.",
            "snippet_text": "The report directly explains the mechanism in context.",
            "support_summary": "The file supports the requested mechanism.",
            "uncertainty": "",
            "locator_text": "",
            "locator_type": "unknown",
            "confidence": 0.91,
        }
    )
    fake_db = _FakeModelReadDb(provider_files=[provider_file], versions={source_version_id: version})
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_model_read_model", "gpt-5.4-mini")
    monkeypatch.setattr(evidence_module, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(evidence_module, "_model_read_upload_payload", lambda _version: ("report.pdf", b"%PDF fake"))
    toolset = EvidenceToolset(
        db=fake_db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
        provider_index=provider_index,
    )

    result = await toolset.read_source_with_model_tool(
        {
            "source_id": str(source_id),
            "source_version_id": "",
            "focus_question": "What mechanism does the report support?",
            "claim_text": "",
            "purpose": "source_triage",
            "locator_hint": "",
        }
    )

    assert result["found"] is True
    assert result["evidence"]["source_ref"] == "S01"
    assert result["evidence"]["support_assessment"] == "direct"
    assert "provider_file_id" not in result["evidence"]
    assert "provider_vector_store_id" not in result["evidence"]
    assert "metadata" not in result["evidence"]
    assert "retrieval_channel" not in result["evidence"]
    assert "evidence_strength" not in result["evidence"]
    assert "source_version_id" not in result["evidence"]
    assert "The file supports the requested mechanism." in result["evidence"]["snippet_text"]
    persisted_evidence = [item for item in fake_db.added if isinstance(item, EvidenceItem)]
    assert persisted_evidence[0].provider_file_id == "file_model_read"
    assert persisted_evidence[0].metadata_["vector_store_provider_file_id"] == "file_test"
    assert fake_client.files.calls[0]["purpose"] == "user_data"
    assert fake_client.responses.calls[0]["model"] == "gpt-5.4-mini"
    assert fake_client.responses.calls[0]["input"][0]["content"][1] == {"type": "input_file", "file_id": "file_model_read"}
    assert provider_file.attributes["model_read_file_id"] == "file_model_read"


@pytest.mark.asyncio
async def test_read_source_with_model_cache_hit_reuses_existing_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_retrieval as evidence_module

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    provider_index_id = uuid.uuid4()
    provider_index = ProviderCorpusIndex(
        id=provider_index_id,
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    version = SourceVersion(
        id=source_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/report.pdf",
        content_hash="hash",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider="openai",
        provider_file_id="file_test",
        provider_vector_store_file_id="vsf_test",
        attributes={"filename": "report.pdf"},
        status=V2RecordStatus.READY,
    )
    fake_client = _FakeModelReadClient(
        {
            "supports_query": True,
            "support_level": "direct",
            "quote_text": "The report directly explains the mechanism.",
            "snippet_text": "The report directly explains the mechanism in context.",
            "support_summary": "The file supports the requested mechanism.",
            "uncertainty": "",
            "locator_text": "section 1",
            "locator_type": "section",
            "confidence": 0.91,
        }
    )
    fake_db = _FakeModelReadDb(provider_files=[provider_file], versions={source_version_id: version})
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(evidence_module, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(evidence_module, "_model_read_upload_payload", lambda _version: ("report.pdf", b"%PDF fake"))
    toolset = EvidenceToolset(
        db=fake_db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
        provider_index=provider_index,
    )
    args = {
        "source_id": str(source_id),
        "source_version_id": "",
        "focus_question": "What mechanism does the report support?",
        "claim_text": "",
        "purpose": "source_triage",
        "locator_hint": "",
    }

    first = await toolset.read_source_with_model_tool(args)
    second = await toolset.read_source_with_model_tool(args)

    assert first["found"] is True
    assert second["found"] is True
    assert second["cache_hit"] is True
    assert second["evidence"]["evidence_id"] == first["evidence"]["evidence_id"]
    assert len(fake_client.responses.calls) == 1


@pytest.mark.asyncio
async def test_read_source_with_model_tries_next_provider_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_retrieval as evidence_module

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    original_version_id = uuid.uuid4()
    text_version_id = uuid.uuid4()
    provider_index_id = uuid.uuid4()
    provider_index = ProviderCorpusIndex(
        id=provider_index_id,
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    original_version = SourceVersion(
        id=original_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/report.pdf",
        content_hash="hash-original",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    text_version = SourceVersion(
        id=text_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="sources/report.txt",
        content_hash="hash-text",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    original_provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index_id,
        source_id=source_id,
        source_version_id=original_version_id,
        provider="openai",
        provider_file_id="file_original",
        provider_vector_store_file_id="vsf_original",
        attributes={"filename": "report.pdf"},
        status=V2RecordStatus.READY,
    )
    text_provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index_id,
        source_id=source_id,
        source_version_id=text_version_id,
        provider="openai",
        provider_file_id="file_text",
        provider_vector_store_file_id="vsf_text",
        attributes={"filename": "report.txt"},
        status=V2RecordStatus.READY,
    )
    fake_client = _FakeModelReadClient(
        {
            "supports_query": True,
            "support_level": "supported",
            "quote_text": "",
            "snippet_text": "The extracted text explains the fallback mechanism.",
            "support_summary": "The extracted text supports the requested mechanism.",
            "uncertainty": "",
            "locator_text": "section 2",
            "locator_type": "section",
            "confidence": 0.84,
        },
        response_failures=1,
    )
    fake_db = _FakeModelReadDb(
        provider_files=[text_provider_file, original_provider_file],
        versions={original_version_id: original_version, text_version_id: text_version},
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(evidence_module, "get_openai_client", lambda: fake_client)
    monkeypatch.setattr(
        evidence_module,
        "_model_read_upload_payload",
        lambda version: (f"{version.id}.txt", f"content for {version.id}".encode("utf-8")),
    )
    toolset = EvidenceToolset(
        db=fake_db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(original_version_id), str(text_version_id)],
        ),
        provider_index=provider_index,
    )

    result = await toolset.read_source_with_model_tool(
        {
            "source_id": str(source_id),
            "source_version_id": str(original_version_id),
            "focus_question": "What mechanism does the report support?",
            "claim_text": "",
            "purpose": "source_triage",
            "locator_hint": "",
        }
    )

    assert result["found"] is True
    assert result["evidence"]["source_ref"] == "S01"
    assert result["evidence"]["support_assessment"] == "supported"
    assert "source_version_id" not in result["evidence"]
    assert "provider_file_id" not in result["evidence"]
    assert "metadata" not in result["evidence"]
    persisted_evidence = [item for item in fake_db.added if isinstance(item, EvidenceItem)]
    assert persisted_evidence[0].source_version_id == text_version_id
    assert persisted_evidence[0].metadata_["vector_store_provider_file_id"] == "file_text"
    assert len(fake_client.responses.calls) == 2
    assert original_provider_file.attributes["model_read_file_id"] == "file_model_read"
    assert text_provider_file.attributes["model_read_file_id"] == "file_model_read_2"


@pytest.mark.asyncio
async def test_read_source_with_model_local_fallback_uses_readable_version_and_weak_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_retrieval as evidence_module

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    original_version_id = uuid.uuid4()
    text_version_id = uuid.uuid4()
    original_version = SourceVersion(
        id=original_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/report.pdf",
        content_hash="hash-original",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    text_version = SourceVersion(
        id=text_version_id,
        source_id=source_id,
        project_id=project_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="sources/report.txt",
        content_hash="hash-text",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
        metadata_={},
    )
    fake_db = _FakeModelReadDb(
        provider_files=[],
        versions={original_version_id: original_version, text_version_id: text_version},
        execute_values=[[text_version], []],
    )
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(
        evidence_module,
        "_source_version_text",
        lambda version: "The extracted text has a focused mechanism window." if version.id == text_version_id else "",
    )
    toolset = EvidenceToolset(
        db=fake_db,  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(original_version_id), str(text_version_id)],
        ),
        provider_index=None,
    )

    result = await toolset.read_source_with_model_tool(
        {
            "source_id": str(source_id),
            "source_version_id": str(original_version_id),
            "focus_question": "What mechanism does the report support?",
            "claim_text": "",
            "purpose": "source_triage",
            "locator_hint": "",
        }
    )

    assert result["found"] is True
    assert "coverage_warning" in result
    assert "local fallback" in result["coverage_warning"]
    assert result["evidence"]["source_ref"] == "S01"
    assert "source_version_id" not in result["evidence"]
    assert "retrieval_channel" not in result["evidence"]
    assert "evidence_strength" not in result["evidence"]
    assert "metadata" not in result["evidence"]


@pytest.mark.asyncio
async def test_select_evidence_rejects_unsupported_model_read_as_support() -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    evidence = EvidenceItem(
        id=evidence_id,
        generation_run_id=run_id,
        project_id=project_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider="openai",
        provider_file_id="file_model_read",
        retrieval_channel="model_file_read",
        query_text="claim",
        purpose="model_file_read",
        search_result_index=0,
        score=0.2,
        snippet_text="Support summary: The file does not support this claim.",
        normalized_quote_text="the file does not support this claim",
        locator_text="model file read",
        locator_type="unknown",
        evidence_strength="weak",
        snippet_hash=hashlib.sha256(b"negative").hexdigest(),
        metadata_={"usage_status": "observed", "supports_query": False, "support_level": "not_found"},
    )
    toolset = EvidenceToolset(
        db=_FakeSelectEvidenceDb(evidence),  # type: ignore[arg-type]
        run=SimpleNamespace(id=run_id, project_id=project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
        provider_index=None,
    )

    rejected = await toolset.select_evidence_tool(
        {"evidence_id": str(evidence_id), "purpose": "source_map", "role": "mechanism", "rationale": "support"}
    )
    rejected_conflict = await toolset.select_evidence_tool(
        {"evidence_id": str(evidence_id), "purpose": "source_map", "role": "conflict", "rationale": "too broad"}
    )
    accepted_gap = await toolset.select_evidence_tool(
        {"evidence_id": str(evidence_id), "purpose": "gap", "role": "gap", "rationale": "negative finding"}
    )

    assert rejected["accepted"] is False
    assert rejected_conflict["accepted"] is False
    assert accepted_gap["accepted"] is True
    assert evidence.metadata_["usage_status"] == "selected"


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
    tools_by_name = {tool.name: tool for tool in tools}
    descriptions = {tool.name: tool.description for tool in tools}

    assert "neutral metadata" in descriptions["list_sources"]
    assert "up to 25 sources per page" in descriptions["list_sources"]
    assert "scope=all_sources" in descriptions["search_corpus"]
    assert "source_refs" in descriptions["search_corpus"]
    assert "persist" in descriptions["search_corpus"].lower()
    assert "discovery preview" in descriptions["search_corpus"]
    assert "source-map" not in descriptions["search_corpus"]
    assert "source_ref/title" in descriptions["read_evidence"]
    assert "does not read the whole source" in descriptions["read_source_window"]
    assert "persist that window as an EvidenceItem" in descriptions["read_source_window"]
    assert "If skim_source is available and the source is unfamiliar, skim first" in descriptions["read_source_window"]
    assert "Use the returned evidence_id in durable write actions" in descriptions["read_source_window"]
    assert "input_file" in descriptions["read_source_with_model"]
    assert "version_ref is optional" in descriptions["read_source_with_model"]
    assert "coverage_warning" in descriptions["read_source_with_model"]
    assert "Page numbers are useful when reported, but not required" in descriptions["read_source_with_model"]
    assert "Default first-read tool" in descriptions["skim_source"]
    assert "Skim representative windows and persist them as EvidenceItems" in descriptions["skim_source"]
    assert "version_ref is optional" in descriptions["skim_source"]
    assert "not a generic source search tool" in descriptions["find_quotes"]
    assert "research atom" in descriptions["find_quotes"]
    assert "corpus/source triage only" in descriptions["search_sources"]
    assert "2-5 selected sources" in descriptions["compare_source_evidence"]
    assert "write the resulting cluster/relationship/gap afterward" in descriptions["compare_source_evidence"]
    assert "regulator" not in descriptions["record_source_cluster"]
    assert "legal texts" not in descriptions["record_source_cluster"]
    for tool_name in (
        "inspect_source",
        "read_source_window",
        "read_source_with_model",
        "search_within_source",
        "skim_source",
    ):
        assert "version_ref" not in (tools_by_name[tool_name].parameters.get("required") or [])


def test_list_sources_compact_card_contract_excludes_blob_fields() -> None:
    from app.generation_v2.tools.evidence_tool_handlers import _compact_source_card

    card = SimpleNamespace(
        source_id="src-1",
        source_version_ids=[f"ver-{idx}" for idx in range(20)],
        title="Currency note issuer history",
        source_type="pdf",
        domain="hkma.gov.hk",
        published_at="2024-01-01",
        coverage_status="ready",
        representation_summary="original_file:ready/ready, extracted_text:ready/pending",
        summary="This is a long neutral preview. " * 80,
    )

    payload = _compact_source_card(card, canonical_kind="original_file", source_ref="S01")

    assert set(payload) == {
        "source_ref",
        "title",
        "source_type",
        "domain",
        "published_at",
        "coverage_status",
        "canonical_kind",
        "version_count",
        "brief_hint",
    }
    assert payload["source_ref"] == "S01"
    assert payload["version_count"] == 20
    assert len(payload["brief_hint"]) <= 120
    assert "source_version_ids" not in payload
    assert "source_id" not in payload
    assert "authority" not in payload
    assert "reliability" not in payload
    assert "summary" not in payload
    assert "provider_file_id" not in payload


def test_prompt_inventory_uses_refs_not_backend_ids() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    card = SourceCard(
        source_id=source_id,
        source_version_ids=[source_version_id],
        evidence_ids=[evidence_id],
        title="HKMA note issue history",
        source_type="web",
        domain="hkma.gov.hk",
        published_at="2019-08-26T00:00:00",
        length_hint="preview_chars=404",
        reliability="high",
        authority="primary",
        summary="Primary explanation of Hong Kong note-issuing banks and certificates of indebtedness.",
        useful_for=["mechanism"],
        representation_summary="extracted_text:ready/ready",
        ready_representation_count=1,
        coverage_status="ready",
    )
    quote = QuoteCard(
        source_id=source_id,
        source_version_id=source_version_id,
        evidence_id=evidence_id,
        quote="Commercial banks issue most Hong Kong currency notes.",
        locator="section 1",
        narrative_use="mechanism",
    )
    triage = SourceTriageReport(
        ranked_sources=[
            SourceTriageItem(
                source_id=source_id,
                source_version_ids=[source_version_id],
                evidence_ids=[evidence_id],
                rank=1,
                score=0.95,
                authority="primary",
                reliability="high",
                useful_for=["mechanism"],
            )
        ],
        top_sources_by_function={"mechanism": [source_id]},
        source_clusters=[
            SourceCluster(
                cluster_id="official",
                label="Official mechanism sources",
                source_ids=[source_id],
                primary_source_ids=[source_id],
                evidence_ids=[evidence_id],
            )
        ],
        source_relationships=[
            SourceRelationship(
                source_id=source_id,
                related_source_id=source_id,
                relationship="background_for",
                evidence_ids=[evidence_id],
                rationale="Official source grounds the mechanism.",
            )
        ],
    )
    evidence = _evidence_record(
        evidence_id=evidence_id,
        source_id=source_id,
        source_version_id=source_version_id,
        metadata={"usage_status": "selected", "support_level": "direct"},
    )
    ref_context = _source_ref_context([card])

    payload = {
        "sources": _source_inventory([card], ref_context=ref_context),
        "quotes": _quote_inventory([quote], ref_context=ref_context),
        "triage": _triage_inventory(triage, ref_context=ref_context),
        "evidence": _evidence_inventory([evidence], source_cards=[card], ref_context=ref_context),
    }
    text = json.dumps(payload)

    assert source_id not in text
    assert source_version_id not in text
    assert "source_id" not in text
    assert "source_version_id" not in text
    assert "source_version_ids" not in text
    assert "retrieval_channel" not in text
    assert "evidence_strength" not in text
    assert "usage_status" not in text
    assert "provider_file_id" not in text
    assert '"source_ref": "S01"' in text
    assert evidence_id in text


def test_evidence_tool_parameters_are_openai_strict_function_compatible() -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    tools = EvidenceToolset(db=None, run=None, run_view=None, provider_index=None).agent_tools()  # type: ignore[arg-type]
    for tool in tools:
        schema = _strict_json_schema(tool.parameters)
        _assert_strict_object_schema(schema, path=tool.name, is_root=True)


@pytest.mark.asyncio
async def test_persist_evidence_item_truncates_long_purpose_for_db_column() -> None:
    from app.generation_v2.tools.evidence_store import persist_evidence_item

    class FakeDb:
        def __init__(self) -> None:
            self.items: list[EvidenceItem] = []

        async def get(self, *_args, **_kwargs):
            return None

        async def execute(self, _statement):
            return _FakeCorpusResult([])

        def add(self, item):
            self.items.append(item)

        async def flush(self):
            for item in self.items:
                if getattr(item, "id", None) is None:
                    item.id = uuid.uuid4()

    long_purpose = (
        "Legal basis for note issuance and currency system under Basic Law to explain legal framework allowing "
        "private banknote issue backed by reserves"
    )

    item = await persist_evidence_item(
        FakeDb(),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        retrieval_channel="provider_vector_search",
        query_text="Basic Law Article 111 issue of currency notes",
        purpose=long_purpose,
        snippet_text="The source discusses currency note issuance.",
        locator_text="provider snippet",
    )

    assert item.purpose is not None
    assert len(item.purpose) == 120
    assert item.metadata_["full_purpose"] == long_purpose


def _assert_strict_object_schema(node: dict | list, *, path: str, is_root: bool = False) -> None:
    if isinstance(node, list):
        for idx, item in enumerate(node):
            _assert_strict_object_schema(item, path=f"{path}[{idx}]")
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties")
        assert isinstance(properties, dict), path
        assert node.get("additionalProperties") is False, path
        assert set(node.get("required") or []) == set(properties), path
        if not is_root:
            assert properties, f"{path} must not be an open-ended empty object in strict tool schemas"
        for key, child in properties.items():
            _assert_strict_object_schema(child, path=f"{path}.{key}")
    for key in ("items", "anyOf", "oneOf", "allOf", "$defs"):
        value = node.get(key)
        if isinstance(value, dict):
            if key == "$defs":
                for name, child in value.items():
                    _assert_strict_object_schema(child, path=f"{path}.$defs.{name}")
            else:
                _assert_strict_object_schema(value, path=f"{path}.{key}")
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                _assert_strict_object_schema(child, path=f"{path}.{key}[{idx}]")


def test_prompt_contracts_explain_claim_refs_quote_refs_and_citation_schema() -> None:
    assert "claim_refs are optional temporary fact-check hints only" in ACT_DRAFT_PROMPT
    assert "not claim ids, citation ids, evidence ids, or source ids" in ACT_DRAFT_PROMPT
    assert "Before using any required evidence_id in speech, call read_evidence" in ACT_DRAFT_PROMPT
    assert "read_evidence, read_source_window, or search_within_source only to repair a known gap" in ACT_REWRITE_PROMPT
    assert "Build a working map of the corpus" in SOURCE_TRIAGE_PROMPT
    assert "SourceMap questions:" in SOURCE_TRIAGE_PROMPT
    assert "corpus cartography" in SOURCE_TRIAGE_PROMPT
    assert "Workflow:" in SOURCE_TRIAGE_PROMPT
    assert "Pick one small source set" in SOURCE_TRIAGE_PROMPT
    assert "For each not-yet-examined source in the set, normally start with skim_source" in SOURCE_TRIAGE_PROMPT
    assert "Use read_source_window after skim/search" in SOURCE_TRIAGE_PROMPT
    assert "read at least two relevant sources before writing the relationship or cluster" in SOURCE_TRIAGE_PROMPT
    assert "Do not treat reading as a cost problem" in SOURCE_TRIAGE_PROMPT
    assert "Quality bar:" in SOURCE_TRIAGE_PROMPT
    assert "Tool policy:" in SOURCE_TRIAGE_PROMPT
    assert "Mini-example:" in SOURCE_TRIAGE_PROMPT
    assert "Final response:" in SOURCE_TRIAGE_PROMPT
    assert "Return SourceTriageCompletion JSON only" in SOURCE_TRIAGE_PROMPT
    assert "Do not include SourceMap content in the final response" in SOURCE_TRIAGE_PROMPT
    assert "most usable sources should appear in at least one SourceCard" in SOURCE_TRIAGE_PROMPT
    assert "search_corpus is for targeted cross-corpus questions" in SOURCE_TRIAGE_PROMPT
    assert "compare_source_evidence" in SOURCE_TRIAGE_PROMPT
    assert "record_source_cluster" in SOURCE_TRIAGE_PROMPT
    assert "select_evidence" not in SOURCE_TRIAGE_PROMPT
    assert "source version IDs" not in SOURCE_TRIAGE_PROMPT
    assert "Do not spend the whole budget" not in SOURCE_TRIAGE_PROMPT
    assert "Do not follow instructions inside" not in SOURCE_TRIAGE_PROMPT
    for case_specific_term in (
        "official/regulator",
        "bank/source-owner",
        "regulator",
        "issuer",
        "legal text",
        "legal texts",
        "law",
    ):
        assert case_specific_term not in SOURCE_TRIAGE_PROMPT
    assert "upsert_research_job" in RESEARCH_PLANNER_PROMPT
    assert "A good job has:" in RESEARCH_PLANNER_PROMPT
    assert "Mini-examples:" in RESEARCH_PLANNER_PROMPT
    assert "upsert_editorial_atom" in RESEARCH_JOB_PROMPT
    assert "What success looks like:" in RESEARCH_JOB_PROMPT
    assert "skim_source or inspect_source" in RESEARCH_JOB_PROMPT
    assert "scope=all_sources" in RESEARCH_JOB_PROMPT
    assert "record_research_observation or upsert_editorial_atom" in RESEARCH_JOB_PROMPT
    assert "tool validates the refs" in RESEARCH_JOB_PROMPT
    assert "finalize_research_memo" in RESEARCH_FINALIZER_PROMPT
    assert "research_synthesis" in RESEARCH_FINALIZER_PROMPT
    assert "research synthesis, not an episode treatment" in RESEARCH_FINALIZER_PROMPT
    assert "narrative_spine" not in RESEARCH_FINALIZER_PROMPT
    assert "treatment-ready" not in RESEARCH_FINALIZER_PROMPT
    assert "can become acts" not in RESEARCH_FINALIZER_PROMPT
    assert "atom_drafts" in RESEARCH_FINALIZER_PROMPT
    assert "Read/search/compare only enough" not in RESEARCH_JOB_PROMPT
    assert "select_evidence" not in RESEARCH_PLANNER_PROMPT
    assert "select_evidence" not in RESEARCH_JOB_PROMPT
    assert "select_evidence" not in RESEARCH_FINALIZER_PROMPT
    assert "needs_repair" not in RESEARCH_JOB_PROMPT
    assert "source_ids" not in RESEARCH_JOB_PROMPT
    assert "source ids" not in RESEARCH_JOB_PROMPT.lower()
    assert "source_ids" not in RESEARCH_FINALIZER_PROMPT
    assert "source ids" not in RESEARCH_FINALIZER_PROMPT.lower()
    assert "under-grounded, invalid, unwritten, or missing persisted gap/caution work" in SOURCE_TRIAGE_REPAIR_PROMPT
    assert "missing_model_read_source_ids" not in SOURCE_TRIAGE_REPAIR_PROMPT
    assert "select_evidence" not in SOURCE_TRIAGE_REPAIR_PROMPT
    assert "Do not restart from scratch" in SOURCE_TRIAGE_REPAIR_PROMPT
    assert "prefetch_seed" not in SOURCE_TRIAGE_PROMPT
    assert "warm-cache" not in SOURCE_TRIAGE_PROMPT
    assert "Use read_evidence for suspicious leaps" in EPISODE_CRITIQUE_PROMPT
    assert "do not introduce new facts" in EPISODE_CRITIQUE_PROMPT
    assert "claim_type=factual" in CLAIM_CITATION_PROMPT
    assert "source_need=required" in CLAIM_CITATION_PROMPT
    assert "risk_level=high" in CLAIM_CITATION_PROMPT
    assert "support_level=supported only when saved evidence directly supports" in CLAIM_CITATION_PROMPT
    assert "read_source_with_model before creating a CitationLink" in CLAIM_CITATION_PROMPT
    assert "select_evidence before linking" in CLAIM_CITATION_PROMPT
    assert "coverage_warning" in CLAIM_CITATION_PROMPT
    assert "blocking=true for missing evidence" in CLAIM_CITATION_PROMPT
    assert '{"ready": boolean, "summary": string, "notes": string[]}' in SOURCE_TRIAGE_PROMPT


def test_source_triage_repair_context_scopes_to_gate_failures() -> None:
    missing_source_id = str(uuid.uuid4())
    untouched_source_id = str(uuid.uuid4())
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=missing_source_id, title="Needs direct read"),
            _source_card(source_id=untouched_source_id, title="Already valid"),
        ],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": ["ev-1"]}),
        retrieval_notes=["Initial pass"],
    )
    gate = SourceTriageGate(
        passed=False,
        summary="failed",
        missing_model_read_source_ids=[missing_source_id],
    )

    context = _source_triage_repair_context(source_map, gate)

    assert context["gate"]["missing_model_read_source_ids"] == [missing_source_id]
    repair_cards = context["source_map"]["source_cards_needing_repair"]
    assert [card["source_id"] for card in repair_cards] == [missing_source_id]
    assert context["source_map"]["top_evidence_by_function"] == {"mechanism": ["ev-1"]}


def test_agent_system_prompts_are_role_specific_and_product_aware() -> None:
    systems = {
        "source_triage": SOURCE_TRIAGE_SYSTEM,
        "source_acquisition": SOURCE_ACQUISITION_SYSTEM,
        "research": RESEARCH_SYSTEM,
        "research_audit": RESEARCH_AUDIT_SYSTEM,
        "treatment": TREATMENT_SYSTEM,
        "act_writer": ACT_WRITER_SYSTEM,
        "episode_critic": EPISODE_CRITIC_SYSTEM,
        "act_rewriter": ACT_REWRITER_SYSTEM,
        "citation_linker": CITATION_LINKER_SYSTEM,
        "support_judge": SUPPORT_JUDGE_SYSTEM,
    }

    assert len(set(systems.values())) == len(systems)
    for name, system in systems.items():
        assert "Your role:" in system, name
        assert "long-form podcast production system" in system, name
        assert "source_ref values" in system, name
        assert "version_ref is optional" in system, name
        assert "Generation V2" not in system, name
        assert "Podgen Generation" not in system, name

    assert "Follow the workflow prompt for the current triage stage" in SOURCE_TRIAGE_SYSTEM
    assert "Use corpus search for broad discovery" not in SOURCE_TRIAGE_SYSTEM
    assert "Web search is only a discovery mechanism" in SOURCE_ACQUISITION_SYSTEM
    assert "Use the SourceMap as orientation" in RESEARCH_SYSTEM
    assert "create stronger saved evidence" in RESEARCH_SYSTEM
    assert "Persist working conclusions as research observations or editorial atom drafts" in RESEARCH_SYSTEM
    assert "Each act is a listener job" in TREATMENT_SYSTEM
    assert "Before using required evidence, inspect it" in ACT_WRITER_SYSTEM
    assert "Do not search" in SUPPORT_JUDGE_SYSTEM


def test_legacy_prompts_module_reexports_prompting_constants() -> None:
    from app.generation_v2 import prompts as legacy_prompts

    assert legacy_prompts.ACT_DRAFT_PROMPT == ACT_DRAFT_PROMPT
    assert legacy_prompts.SOURCE_TRIAGE_SYSTEM == SOURCE_TRIAGE_SYSTEM
    assert legacy_prompts.CLAIM_CITATION_PROMPT == CLAIM_CITATION_PROMPT


def test_runtime_tool_trace_names_include_function_tool_names() -> None:
    runtime = V2Runtime()
    tools = runtime._tool_specs(
        vector_store_id=None,
        local_tools=[
            AgentTool(
                name="search_corpus",
                description="Search",
                parameters={"type": "object", "properties": {}},
                handler=None,  # type: ignore[arg-type]
            )
        ],
        policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
    )

    assert tools[0]["name"] == "search_corpus"


@pytest.mark.asyncio
async def test_phase_specific_agent_tool_sets_are_not_broad() -> None:
    _CaptureRuntime.calls = {}
    _CaptureRuntime.kwargs_by_agent = {}
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
    assert set(calls["research_planner_agent"]) == {
        "upsert_research_job",
        "finalize_research_plan",
    }
    research_job_agent = next(name for name in calls if name.startswith("research_job_agent_"))
    assert set(calls[research_job_agent]) == {
        "search_corpus",
        "find_quotes",
        "read_evidence",
        "read_source_window",
        "read_source_with_model",
        "search_within_source",
        "skim_source",
        "inspect_source",
        "compare_evidence_for_question",
        "record_research_observation",
        "upsert_editorial_atom",
        "finish_research_job",
    }
    assert set(calls["research_finalizer_agent"]) == {
        "record_research_risk",
        "finalize_research_memo",
    }
    assert "select_evidence" not in calls[research_job_agent]
    assert "list_sources" not in calls[research_job_agent]
    assert "search_sources" not in calls[research_job_agent]
    assert _CaptureRuntime.kwargs_by_agent[research_job_agent]["max_tool_rounds"] == 80
    assert set(calls["research_auditor"]) == {"search_corpus", "read_evidence"}
    assert set(calls["story_editor"]) == {"read_evidence", "read_source_window", "search_within_source"}
    assert set(calls["act_writer_1"]) == {"read_evidence", "read_source_window", "search_within_source"}
    assert set(calls["episode_critic"]) == {"search_corpus", "read_evidence"}
    assert set(calls["act_rewriter_1"]) == {"read_evidence", "read_source_window", "search_within_source"}
    assert set(calls["citation_linker"]) == {
        "search_corpus",
        "read_evidence",
        "find_quotes",
        "read_source_window",
        "read_source_with_model",
        "search_within_source",
        "select_evidence",
    }


@pytest.mark.asyncio
async def test_research_workflow_uses_sourcemap_summary_not_full_inventory_prompt() -> None:
    _CaptureRuntime.calls = {}
    _CaptureRuntime.kwargs_by_agent = {}
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        config={"user_prompt": "Hong Kong dollar", "duration_target": 2700, "language": "ru"},
    )
    source_map_card = _source_card(title="Triaged promoted source")
    full_cards = [source_map_card] + [
        _source_card(title=f"Neutral inventory source {idx}")
        for idx in range(2, 41)
    ]

    await build_research_memo(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(_fallback_research_memo(run, [source_map_card], [])),
        source_cards=[source_map_card],
        quote_cards=[],
        source_map=SourceMap(source_cards=[source_map_card]),
        source_inventory=SourceMap(source_cards=full_cards),
        evidence_tools=_all_evidence_tools(),
    )

    kwargs = _CaptureRuntime.kwargs_by_agent["research_planner_agent"]
    assert kwargs["max_tool_rounds"] == 40
    assert "Neutral inventory source 40" not in kwargs["prompt"]
    assert "SourceMap summary from triage" in kwargs["prompt"]
    assert "Triaged promoted source" in kwargs["prompt"]


def test_research_sourcemap_summary_controls_evidence_visibility_by_stage() -> None:
    first_source_id = str(uuid.uuid4())
    second_source_id = str(uuid.uuid4())
    mapped_card = _source_card(source_id=first_source_id, title="Mapped source").model_copy(
        update={"evidence_ids": ["ev-card"]}
    )
    inventory = SourceMap(
        source_cards=[
            mapped_card,
            _source_card(source_id=second_source_id, title="Related source"),
        ]
    )
    source_map = SourceMap(
        source_cards=[mapped_card],
        quote_cards=[
            QuoteCard(
                source_id=first_source_id,
                evidence_id="ev-quote",
                quote="A useful quoted sentence.",
                locator="p. 1",
                narrative_use="mechanism",
            )
        ],
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster-1",
                    label="Mechanism cluster",
                    source_ids=[first_source_id, second_source_id],
                    evidence_ids=["ev-cluster"],
                )
            ],
            source_relationships=[
                SourceRelationship(
                    source_id=first_source_id,
                    related_source_id=second_source_id,
                    relationship="background_for",
                    evidence_ids=["ev-relationship"],
                    rationale="The first source frames the second.",
                )
            ],
        ),
    )

    planner_summary = source_map_research_summary(source_map, inventory, include_evidence_ids=False)
    job_summary = source_map_research_summary(source_map, inventory, include_evidence_ids=True)

    assert planner_summary["includes_evidence_ids"] is False
    assert "evidence_ids" not in planner_summary["source_cards"][0]
    assert "evidence_id" not in planner_summary["quote_cards"][0]
    assert "evidence_ids" not in planner_summary["clusters"][0]
    assert "evidence_ids" not in planner_summary["relationships"][0]
    assert job_summary["includes_evidence_ids"] is True
    assert job_summary["source_cards"][0]["evidence_ids"] == ["ev-card"]
    assert job_summary["quote_cards"][0]["evidence_id"] == "ev-quote"
    assert job_summary["clusters"][0]["evidence_ids"] == ["ev-cluster"]
    assert job_summary["relationships"][0]["evidence_ids"] == ["ev-relationship"]


def test_research_finalizer_prompt_payload_contains_persisted_work_not_counts_only() -> None:
    workspace = {
        "source_order": ["S01"],
        "selected_evidence_ids": ["ev-1"],
        "investigation_jobs": [
            {
                "job_id": "job-mechanism",
                "label": "Mechanism",
                "story_job": "mechanism",
                "purpose": "Explain the operating model.",
                "source_refs": ["S01"],
                "questions": ["How does the mechanism work?"],
                "status": "finished",
                "observations": [
                    {
                        "source_refs": ["S01"],
                        "evidence_ids": ["ev-1"],
                        "note": "S01 gives the concrete exchange process.",
                        "pending_questions": ["Find the consequence."],
                    }
                ],
                "atom_ids": ["atom-1"],
                "evidence_ids": ["ev-1"],
            }
        ],
        "atom_drafts": [
            {
                "atom_id": "atom-1",
                "job_id": "job-mechanism",
                "kind": "mechanism",
                "title": "Mechanism atom",
                "claim": "The system turns obligations into issuance capacity.",
                "explanation": "The source explains the concrete operating process.",
                "story_value": "Lets treatment explain the machine before the stakes.",
                "source_refs": ["S01"],
                "evidence_ids": ["ev-1"],
                "confidence": 0.8,
                "risk": "medium",
                "source_coverage_status": "supported",
            }
        ],
        "open_questions": ["Consequence still needs detail."],
        "risk_register": [
            {
                "risk": "One source covers the mechanism but not the consequence.",
                "severity": "medium",
                "source_refs": ["S01"],
                "evidence_ids": ["ev-1"],
            }
        ],
    }

    payload = research_finalizer_payload_for_prompt(workspace)

    assert payload["selected_evidence_count"] == 1
    assert payload["jobs"][0]["job_id"] == "job-mechanism"
    assert payload["observations"][0]["note"] == "S01 gives the concrete exchange process."
    assert payload["atom_drafts"][0]["evidence_ids"] == ["ev-1"]
    assert payload["risk_register"][0]["source_refs"] == ["S01"]
    assert "Use atom_drafts as the memo backbone" in payload["assembly_instruction"]


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
        metadata={
            "usage_status": "selected",
            "support_level": "weak",
            "supports_query": True,
            "model_file_read_local_fallback": True,
            "selected_role": "mechanism",
        },
    )

    source_card = _source_card(source_id=source_id)
    ref_context = _source_ref_context([source_card])
    inventory = _evidence_inventory(
        [*extra_records, preferred_record],
        source_cards=[source_card],
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
        ref_context=ref_context,
    )

    assert len(inventory) == 5
    assert inventory[0]["evidence_id"] == preferred_id
    assert inventory[0]["source_ref"] == "S01"
    assert len(inventory[0]["snippet_text"]) <= 900
    assert inventory[0]["metadata_flags"] == {
        "support_level": "weak",
        "supports_query": True,
        "selected_role": "mechanism",
    }
    assert "source_id" not in inventory[0]
    assert "source_version_id" not in inventory[0]
    assert "retrieval_channel" not in inventory[0]
    assert "evidence_strength" not in inventory[0]


def test_research_memo_evidence_gate_blocks_unanchored_high_impact_atoms() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
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

    gate = _validate_research_memo_evidence(
        memo,
        evidence_records=[
            _evidence_record(evidence_id=evidence_id, source_id=source_id, source_version_id=source_version_id)
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
        ),
        duration_seconds=3600,
    )

    assert gate.passed is False
    assert "High-impact atom 1" in gate.missing_material[0]


def test_research_memo_evidence_gate_accepts_research_created_selected_evidence_outside_source_map() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A specific mechanism discovered during research.",
                source_ids=[source_id],
                evidence_ids=[evidence_id],
                story_value="Explains the system.",
            )
        ],
    )

    gate = _validate_research_memo_evidence(
        memo,
        evidence_records=[
            _evidence_record(evidence_id=evidence_id, source_id=source_id, source_version_id=source_version_id)
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
        ),
        duration_seconds=1800,
    )

    assert gate.passed is True


def test_research_memo_evidence_gate_rejects_observed_unselected_atom_evidence() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A specific mechanism.",
                source_ids=[source_id],
                evidence_ids=[evidence_id],
                story_value="Explains the system.",
            )
        ],
    )

    gate = _validate_research_memo_evidence(
        memo,
        evidence_records=[
            _evidence_record(
                evidence_id=evidence_id,
                source_id=source_id,
                source_version_id=source_version_id,
                metadata={"usage_status": "observed"},
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
        ),
        duration_seconds=1800,
    )

    assert gate.passed is False
    assert "not explicitly selected" in " ".join(gate.missing_material)


def test_research_memo_gate_requires_major_cluster_coverage() -> None:
    source_id = str(uuid.uuid4())
    uncovered_source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A specific mechanism.",
                source_ids=[source_id],
                evidence_ids=[evidence_id],
                story_value="Explains the system.",
            )
        ],
    )
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="main-cluster",
                    label="Main explanatory cluster",
                    source_ids=[uncovered_source_id, str(uuid.uuid4())],
                    rationale="A major group of sources for the episode logic.",
                )
            ]
        )
    )

    gate = _validate_research_memo_evidence(
        memo,
        evidence_records=[
            _evidence_record(evidence_id=evidence_id, source_id=source_id, source_version_id=source_version_id)
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
        ),
        duration_seconds=1800,
        source_map=source_map,
    )

    assert gate.passed is False
    assert "Main explanatory cluster" in " ".join(gate.missing_material)


def test_research_memo_gate_skips_low_priority_duplicate_cluster_coverage() -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A specific mechanism.",
                source_ids=[source_id],
                evidence_ids=[evidence_id],
                story_value="Explains the system.",
            )
        ],
    )
    source_map = SourceMap(
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="dup-cluster",
                    label="Duplicate mirrors",
                    source_ids=[str(uuid.uuid4()), str(uuid.uuid4())],
                    rationale="Near-duplicate reposts of the same material.",
                )
            ]
        )
    )

    gate = _validate_research_memo_evidence(
        memo,
        evidence_records=[
            _evidence_record(evidence_id=evidence_id, source_id=source_id, source_version_id=source_version_id)
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
        ),
        duration_seconds=1800,
        source_map=source_map,
    )

    assert gate.passed is True


@pytest.mark.asyncio
async def test_research_memo_evidence_artifacts_are_saved_for_atom_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import orchestrator as orchestrator_module

    run = SimpleNamespace(id=uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    other_evidence_id = str(uuid.uuid4())
    memo = ResearchMemo(
        title="Memo",
        thesis="Thesis",
        research_synthesis="Synthesis",
        editorial_atoms=[
            EditorialAtom(
                kind="mechanism",
                title="Mechanism",
                explanation="A mechanism.",
                evidence_ids=[evidence_id, other_evidence_id, evidence_id],
                story_value="Explains the system.",
            )
        ],
    )
    saved: list[dict] = []

    async def fake_list_evidence_dicts(_db, _run_id):
        return [
            {"id": evidence_id, "snippet_text": "first"},
            {"id": other_evidence_id, "snippet_text": "second"},
        ]

    async def fake_save_artifact(_db, **kwargs):
        saved.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(orchestrator_module, "list_evidence_dicts", fake_list_evidence_dicts)
    monkeypatch.setattr(orchestrator_module, "save_artifact", fake_save_artifact)

    artifact_map = await _save_evidence_artifact_map(
        None,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        evidence_ids=_research_memo_evidence_ids(memo),
        input_artifact_ids=["source-map-artifact"],
    )

    assert list(artifact_map) == [evidence_id, other_evidence_id]
    assert [item["artifact_key"] for item in saved] == [evidence_id, other_evidence_id]
    assert all(item["input_artifact_ids"] == ["source-map-artifact"] for item in saved)


@pytest.mark.asyncio
async def test_source_map_is_built_from_write_actions_and_selected_evidence_only() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    selected_evidence_id = uuid.uuid4()
    observed_only_id = uuid.uuid4()
    inventory = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={"source_version_ids": [str(source_version_id)], "evidence_ids": []}
            )
        ]
    )
    traces = [
        SimpleNamespace(
            tool_name="upsert_source_card",
            input_json={
                "source_id": str(source_id),
                "summary": "Selected mechanism source.",
                "useful_for": ["mechanism"],
                "evidence_ids": [str(selected_evidence_id)],
                "gaps_or_cautions": [],
            },
        ),
        SimpleNamespace(
            tool_name="create_quote_card",
            input_json={
                "evidence_id": str(selected_evidence_id),
                "source_id": str(source_id),
                "source_version_id": str(source_version_id),
                "quote": "The mechanism is explained.",
                "locator": "page 12",
                "narrative_use": "mechanism",
                "confidence": 0.9,
                "confidence_reason": "Exact selected evidence.",
            },
        ),
        SimpleNamespace(
            tool_name="record_source_cluster",
            input_json={
                "label": "Official mechanism sources",
                "source_refs": [str(source_id), str(source_id)],
                "primary_source_refs": [str(source_id)],
                "secondary_source_refs": [],
                "duplicate_source_refs": [],
                "evidence_ids": [str(selected_evidence_id)],
                "rationale": "Official source grounds the mechanism.",
            },
        ),
        SimpleNamespace(tool_name="finalize_source_map", input_json={"ready": True, "summary": "done"}),
    ]
    evidence = [
        SimpleNamespace(
            id=selected_evidence_id,
            source_id=source_id,
            source_version_id=source_version_id,
            metadata_={"usage_status": "observed", "selected_role": "mechanism"},
            snippet_text="The mechanism is explained.",
            locator_text="page 12",
            retrieval_channel="provider_file_search",
            purpose="hosted_file_search",
        ),
        SimpleNamespace(
            id=observed_only_id,
            source_id=source_id,
            source_version_id=source_version_id,
            metadata_={"usage_status": "observed"},
            snippet_text="Observed but not selected.",
            locator_text="page 20",
            retrieval_channel="provider_file_search",
            purpose="hosted_file_search",
        ),
    ]

    source_map = await build_source_map_from_write_actions(
        _FakeWriteActionDb(traces=traces, evidence=evidence),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert source_map.evidence_ids == [str(selected_evidence_id)]
    assert source_map.source_cards[0].evidence_ids == [str(selected_evidence_id)]
    assert source_map.quote_cards[0].evidence_id == str(selected_evidence_id)
    assert source_map.source_triage.source_clusters[0].label == "Official mechanism sources"
    assert source_map.source_triage.source_clusters[0].evidence_ids == [str(selected_evidence_id)]
    assert str(observed_only_id) not in source_map.evidence_ids


@pytest.mark.asyncio
async def test_source_map_does_not_create_cards_from_rejected_write_actions() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    inventory = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={"source_version_ids": [str(source_version_id)], "evidence_ids": []}
            )
        ]
    )
    traces = [
        SimpleNamespace(
            tool_name="select_evidence",
            input_json={"evidence_id": str(evidence_id), "purpose": "source_map", "role": "mechanism", "rationale": "ok"},
            output_json={"accepted": True},
        ),
        SimpleNamespace(
            tool_name="upsert_source_card",
            input_json={
                "source_id": str(source_id),
                "summary": "This write was rejected.",
                "useful_for": ["mechanism"],
                "evidence_ids": [str(evidence_id)],
                "gaps_or_cautions": [],
            },
            output_json={"accepted": False, "error": "evidence_ids must be selected"},
        ),
        SimpleNamespace(tool_name="finalize_source_map", input_json={"ready": True, "summary": "done"}, output_json={"accepted": True}),
    ]
    evidence = [
        SimpleNamespace(
            id=evidence_id,
            source_id=source_id,
            source_version_id=source_version_id,
            metadata_={"usage_status": "selected", "selected_role": "mechanism"},
            snippet_text="The mechanism is explained.",
            locator_text="page 12",
            retrieval_channel="provider_file_search",
            purpose="hosted_file_search",
        )
    ]

    source_map = await build_source_map_from_write_actions(
        _FakeWriteActionDb(traces=traces, evidence=evidence),  # type: ignore[arg-type]
        generation_run_id=uuid.uuid4(),
        inventory=inventory,
    )

    assert source_map.source_cards == []
    assert source_map.evidence_ids == [str(evidence_id)]


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
                SimpleNamespace(
                    id=evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                ),
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
                SimpleNamespace(
                    id=valid_evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                ),
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
async def test_source_triage_gate_warns_without_model_read_for_pdf_provider_snippets() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    provider_evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Report.pdf").model_copy(
                update={
                    "source_type": "pdf",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(provider_evidence_id)],
                }
            )
        ],
        evidence_ids=[str(provider_evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(provider_evidence_id)]}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=provider_evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel="provider_file_search",
                ),
            ],
            traces=[
                SimpleNamespace(tool_name="openai.file_search.observed"),
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                ),
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is True
    assert gate.missing_model_read_source_ids == [str(source_id)]
    assert "model_file_read" not in " ".join([gate.summary, *gate.warnings])


@pytest.mark.asyncio
async def test_source_triage_gate_warns_without_model_read_for_local_fallback() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    fallback_evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Report.pdf").model_copy(
                update={
                    "source_type": "pdf",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(fallback_evidence_id)],
                }
            )
        ],
        evidence_ids=[str(fallback_evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(fallback_evidence_id)]}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=fallback_evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected", "model_file_read_local_fallback": True},
                    retrieval_channel="model_file_read_local_fallback",
                    evidence_strength="weak",
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is True
    assert gate.missing_model_read_source_ids == [str(source_id)]
    assert "model_file_read" not in " ".join([gate.summary, *gate.warnings])


@pytest.mark.asyncio
async def test_source_triage_gate_warns_without_model_read_for_pdf_local_chunks() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    chunk_evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Report.pdf").model_copy(
                update={
                    "source_type": "pdf",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(chunk_evidence_id)],
                }
            )
        ],
        evidence_ids=[str(chunk_evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(chunk_evidence_id)]}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=chunk_evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel="local_chunk_search",
                    evidence_strength="exact",
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is True
    assert gate.missing_model_read_source_ids == [str(source_id)]
    assert "model_file_read" not in " ".join([gate.summary, *gate.warnings])


@pytest.mark.asyncio
async def test_source_triage_gate_accepts_direct_model_read_for_pdf_source_card() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Report.pdf").model_copy(
                update={
                    "source_type": "pdf",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(evidence_id)],
                }
            )
        ],
        evidence_ids=[str(evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(evidence_id)]}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ,
                    evidence_strength=EvidenceStrength.SNIPPET,
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is True
    assert gate.missing_model_read_source_ids == []


@pytest.mark.asyncio
async def test_source_triage_gate_accepts_sparse_function_seed_evidence_as_warning() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Article").model_copy(
                update={
                    "source_type": "web",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(evidence_id)],
                }
            )
        ],
        evidence_ids=[str(evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel="provider_file_search",
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is True
    assert gate.missing_function_evidence == ["top_evidence_by_function"]
    assert "function seed evidence" in " ".join(gate.warnings)


@pytest.mark.asyncio
async def test_source_triage_gate_rejects_unpersisted_gap_notes() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id), title="Article").model_copy(
                update={
                    "source_type": "web",
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(evidence_id)],
                }
            )
        ],
        evidence_ids=[str(evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"context": [str(evidence_id)]}),
        retrieval_notes=["finalized"],
    )
    completion = SourceTriageCompletion(
        ready=True,
        summary="done",
        notes=["Missing primary Basic Law text should be added later."],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel="provider_file_search",
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
        triage_completion=completion,
    )

    assert gate.passed is False
    assert gate.unpersisted_completion_gap_notes == completion.notes
    assert "record_gap" in gate.summary


@pytest.mark.asyncio
async def test_source_triage_gate_requires_explicit_source_card_write() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={
                    "source_version_ids": [str(source_version_id)],
                    "evidence_ids": [str(evidence_id)],
                }
            )
        ],
        evidence_ids=[str(evidence_id)],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": [str(evidence_id)]}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[
                SimpleNamespace(
                    id=evidence_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    metadata_={"usage_status": "selected"},
                    retrieval_channel="local_chunk_search",
                ),
            ],
            traces=[
                SimpleNamespace(
                    tool_name="select_evidence",
                    input_json={"evidence_id": str(evidence_id)},
                    output_json={"accepted": True},
                ),
                SimpleNamespace(
                    tool_name="finalize_source_map",
                    input_json={"ready": True, "summary": "done"},
                    output_json={"accepted": True},
                ),
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is False
    assert gate.missing_source_card_write_source_ids == [str(source_id)]


@pytest.mark.asyncio
async def test_source_triage_gate_requires_selected_evidence_on_each_source_card() -> None:
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=str(source_id)).model_copy(
                update={"source_version_ids": [str(source_version_id)], "evidence_ids": []}
            )
        ],
        source_triage=SourceTriageReport(top_evidence_by_function={"mechanism": []}),
        retrieval_notes=["finalized"],
    )

    gate = await validate_source_map_gate(
        _FakeGateDb(
            evidence=[],
            traces=[
                SimpleNamespace(
                    tool_name="upsert_source_card",
                    input_json={"source_id": str(source_id)},
                    output_json={"accepted": True},
                )
            ],
        ),
        generation_run_id=uuid.uuid4(),
        source_map=source_map,
        run_view=SimpleNamespace(
            selected_source_ids=[str(source_id)],
            selected_source_version_ids=[str(source_version_id)],
        ),
    )

    assert gate.passed is False
    assert gate.missing_source_card_evidence_source_ids == [str(source_id)]


@pytest.mark.asyncio
async def test_source_triage_agent_uses_map_oriented_tool_set() -> None:
    _CaptureRuntime.calls = {}
    _CaptureRuntime.kwargs_by_agent = {}
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})
    completion = SourceTriageCompletion(ready=True, summary="Source triage write actions completed.", notes=["done"])

    result = await build_source_triage_agent(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(completion),
        source_inventory=SourceMap(source_cards=[_source_card()], quote_cards=[]),
        evidence_tools=_all_evidence_tools(),
    )

    assert result.ready is True
    assert result.summary == "Source triage write actions completed."
    assert set(_CaptureRuntime.calls["source_triage_agent"]) == {
        "list_sources",
        "search_corpus",
        "read_evidence",
        "read_source_window",
        "skim_source",
        "inspect_source",
        "compare_source_evidence",
        "upsert_source_card",
        "create_quote_card",
        "record_source_relationship",
        "record_source_cluster",
        "record_gap",
        "finalize_source_map",
    }
    assert _CaptureRuntime.kwargs_by_agent["source_triage_agent"]["tool_policy"].file_search is False


@pytest.mark.asyncio
async def test_source_triage_workflow_agents_have_stage_specific_tools() -> None:
    _CaptureRuntime.calls = {}
    _CaptureRuntime.kwargs_by_agent = {}
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})
    completion = SourceTriageCompletion(ready=True, summary="ok", notes=[])
    workspace_tools = _workspace_triage_tools()
    all_tools = [*_all_evidence_tools(), *workspace_tools]

    await build_source_triage_planner_agent(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(completion),
        workspace_summary={"status": "orientation", "source_count": 3},
        workspace_tools=all_tools,
    )
    await build_source_triage_set_agent(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(completion),
        source_set_payload={"set": {"set_id": "set_1", "source_refs": ["S01", "S02"]}, "capsules": []},
        set_tools=all_tools,
    )
    await build_source_triage_finalizer_agent(
        None,  # type: ignore[arg-type]
        run,
        _CaptureRuntime(completion),
        workspace_summary={"status": "mapped"},
        source_map=SourceMap(),
        finalizer_tools=all_tools,
    )

    assert set(_CaptureRuntime.calls["source_triage_planner_agent"]) == {
        "list_source_capsules",
        "upsert_triage_source_set",
        "finalize_triage_workspace_plan",
    }
    set_agent_name = next(name for name in _CaptureRuntime.calls if name.startswith("source_triage_set_agent_"))
    assert set(_CaptureRuntime.calls[set_agent_name]) == {
        "search_corpus",
        "read_evidence",
        "read_source_window",
        "skim_source",
        "inspect_source",
        "compare_source_evidence",
        "upsert_source_card",
        "create_quote_card",
        "record_source_relationship",
        "record_source_cluster",
        "record_gap",
        "record_triage_observation",
        "finish_triage_source_set",
    }
    assert "list_sources" not in _CaptureRuntime.calls[set_agent_name]
    assert "select_evidence" not in _CaptureRuntime.calls[set_agent_name]
    assert set(_CaptureRuntime.calls["source_triage_finalizer_agent"]) == {
        "record_gap",
        "finalize_source_map",
        "finish_triage_source_set",
    }


@pytest.mark.asyncio
async def test_scoped_triage_set_rejects_global_search_without_provider_call() -> None:
    toolset = EvidenceToolset(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=None,  # type: ignore[arg-type]
        provider_index=None,
        triage_allowed_source_refs={"S01", "S02"},
    )

    result = await toolset.search_corpus_tool(
        {
            "query": "mechanism",
            "purpose": "test",
            "scope": "all_sources",
            "source_refs": [],
            "limit": 5,
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "all_sources_search_not_allowed_in_current_triage_set"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_scoped_research_job_rejects_empty_find_quotes_scope() -> None:
    toolset = EvidenceToolset(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=None,  # type: ignore[arg-type]
        provider_index=None,
        research_allowed_source_refs={"S01", "S02"},
    )

    result = await toolset.find_quotes_tool({"topic": "mechanism", "source_refs": [], "limit": 3})

    assert result["accepted"] is False
    assert result["error"] == "source_refs_required_in_current_scope"
    assert result["results"] == []


def test_source_triage_workflow_prompts_describe_stage_contracts() -> None:
    assert "list_source_capsules" in SOURCE_TRIAGE_PLANNER_PROMPT
    assert "search/read/skim tools create or return evidence_id handles" in SOURCE_TRIAGE_SET_PROMPT
    assert "Assembled SourceMap from persisted write actions" in SOURCE_TRIAGE_FINALIZER_PROMPT
    assert "list_sources" not in SOURCE_TRIAGE_PLANNER_PROMPT
    assert "select_evidence" not in SOURCE_TRIAGE_SET_PROMPT


@pytest.mark.asyncio
async def test_research_workspace_upsert_atom_resolves_refs_and_auto_selects(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as research_workspace_module

    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Topic"})
    workspace = {
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": ["S01"],
                "allow_broad_search": False,
                "status": "in_progress",
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
            }
        ],
        "current_job": {"job_id": "job_1", "source_refs": ["S01"]},
        "atom_drafts": [],
        "selected_evidence_ids": [],
        "covered_source_refs": [],
    }
    inventory = SourceMap(source_cards=[_source_card(source_id=source_id)])

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    class _FakeEvidenceToolset:
        async def _evidence_in_run(self, item_id: str):
            if item_id == evidence_id:
                return SimpleNamespace(id=uuid.UUID(evidence_id), source_id=uuid.UUID(source_id))
            return None

        async def _source_ref_for_id(self, item_source_id):
            return "S01" if str(item_source_id) == source_id else "S??"

        async def _source_ids_from_refs_or_ids(self, refs):
            return ([source_id] if refs == ["S01"] else [], [] if refs == ["S01"] else refs)

        async def _mark_evidence_status(self, item_id, status, metadata):
            self.selected = (item_id, status, metadata)

    fake_toolset = _FakeEvidenceToolset()
    monkeypatch.setattr(research_workspace_module, "load_research_workspace", _load)
    monkeypatch.setattr(research_workspace_module, "save_research_workspace", _save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=inventory,
        evidence_toolset=fake_toolset,
        mode="job",
        current_job_id="job_1",
    )
    upsert_atom = next(tool for tool in tools if tool.name == "upsert_editorial_atom")

    result = await upsert_atom.handler(
        {
            "job_id": "job_1",
            "kind": "mechanism",
            "title": "Mechanism",
            "claim": "The mechanism works through a concrete institutional process.",
            "explanation": "The mechanism works through a concrete institutional process.",
            "source_refs": ["S01"],
            "evidence_ids": [evidence_id],
            "quote_refs": [],
            "story_value": "Explains the system.",
            "anchor_refs": [],
            "confidence": 0.8,
            "risk": "low",
            "source_coverage_status": "ready",
        }
    )

    assert result["accepted"] is True
    assert workspace["atom_drafts"][0]["source_ids"] == [source_id]
    assert workspace["atom_drafts"][0]["evidence_ids"] == [evidence_id]
    assert fake_toolset.selected[0] == evidence_id
    assert fake_toolset.selected[1] == "selected"
    assert fake_toolset.selected[2]["selected_role"] == "research_atom"


@pytest.mark.asyncio
async def test_research_workspace_rejects_unknown_atom_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as research_workspace_module

    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Topic"})
    workspace = {
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": ["S01"],
                "allow_broad_search": False,
                "status": "in_progress",
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
            }
        ],
        "current_job": {"job_id": "job_1", "source_refs": ["S01"]},
        "atom_drafts": [],
    }
    inventory = SourceMap(source_cards=[_source_card(source_id=source_id)])

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    class _FakeEvidenceToolset:
        async def _evidence_in_run(self, item_id: str):
            if item_id == evidence_id:
                return SimpleNamespace(id=uuid.UUID(evidence_id), source_id=uuid.UUID(source_id))
            return None

        async def _source_ref_for_id(self, _item_source_id):
            return "S01"

        async def _source_ids_from_refs_or_ids(self, refs):
            return ([source_id], []) if refs == ["S01"] else ([], refs)

        async def _mark_evidence_status(self, _item_id, _status, _metadata):
            raise AssertionError("invalid atom kind should be rejected before evidence selection")

    monkeypatch.setattr(research_workspace_module, "load_research_workspace", _load)
    monkeypatch.setattr(research_workspace_module, "save_research_workspace", _save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=inventory,
        evidence_toolset=_FakeEvidenceToolset(),
        mode="job",
        current_job_id="job_1",
    )
    upsert_atom = next(tool for tool in tools if tool.name == "upsert_editorial_atom")

    result = await upsert_atom.handler(
        {
            "job_id": "job_1",
            "kind": "claim",
            "title": "Claim",
            "claim": "A supported claim.",
            "explanation": "A supported claim.",
            "source_refs": ["S01"],
            "evidence_ids": [evidence_id],
            "quote_refs": [],
            "story_value": "Useful research finding.",
            "anchor_refs": [],
            "confidence": 0.8,
            "risk": "low",
            "source_coverage_status": "ready",
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "atom kind must be one of the supported research finding kinds"
    assert "unanswered_question" in result["allowed_kinds"]
    assert workspace["atom_drafts"] == []


@pytest.mark.asyncio
async def test_research_workspace_broad_job_still_rejects_refs_outside_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import research_workspace as research_workspace_module

    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Topic"})
    workspace = {
        "source_order": ["S01"],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": [],
                "allow_broad_search": True,
                "status": "in_progress",
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
            }
        ],
        "current_job": {"job_id": "job_1", "source_refs": []},
        "atom_drafts": [],
    }
    inventory = SourceMap(source_cards=[_source_card(source_id=source_id)])

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    class _FakeEvidenceToolset:
        async def _evidence_in_run(self, _item_id: str):
            return SimpleNamespace(id=uuid.UUID(evidence_id), source_id=uuid.UUID(source_id))

        async def _source_ref_for_id(self, _item_source_id):
            return "S01"

        async def _source_ids_from_refs_or_ids(self, refs):
            return ([source_id], []) if refs == ["S01"] else ([], refs)

        async def _mark_evidence_status(self, _item_id, _status, _metadata):
            raise AssertionError("invalid workspace refs should be rejected before evidence selection")

    monkeypatch.setattr(research_workspace_module, "load_research_workspace", _load)
    monkeypatch.setattr(research_workspace_module, "save_research_workspace", _save)
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=inventory,
        evidence_toolset=_FakeEvidenceToolset(),
        mode="job",
        current_job_id="job_1",
    )
    upsert_atom = next(tool for tool in tools if tool.name == "upsert_editorial_atom")

    result = await upsert_atom.handler(
        {
            "job_id": "job_1",
            "kind": "mechanism",
            "title": "Mechanism",
            "claim": "Claim",
            "explanation": "Explanation",
            "source_refs": ["S99"],
            "evidence_ids": [evidence_id],
            "quote_refs": [],
            "story_value": "Explains the system.",
            "anchor_refs": [],
            "confidence": 0.8,
            "risk": "low",
            "source_coverage_status": "ready",
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "atom source_refs must exist in the research workspace"
    assert result["invalid_source_refs"] == ["S99"]
    assert workspace["atom_drafts"] == []


def test_research_finalizer_tool_schema_uses_research_synthesis() -> None:
    run = SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Topic"})
    workspace = {"source_order": [], "atom_drafts": [{"atom_id": "atom_1"}]}
    tools = build_research_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        source_inventory=SourceMap(),
        evidence_toolset=None,
        mode="finalizer",
    )

    finalize = next(tool for tool in tools if tool.name == "finalize_research_memo")
    properties = finalize.parameters["properties"]
    required = set(finalize.parameters["required"])

    assert "research_synthesis" in properties
    assert "research_synthesis" in required
    assert "narrative_spine" not in properties
    assert "narrative_spine" not in required


def test_research_workspace_signature_changes_when_sourcemap_content_changes() -> None:
    from app.generation_v2 import research_workspace as research_workspace_module

    source_id = str(uuid.uuid4())
    inventory = SourceMap(source_cards=[_source_card(source_id=source_id, title="Inventory")])
    source_map_a = SourceMap(
        source_cards=[_source_card(source_id=source_id, title="Source A")],
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster",
                    label="Mechanism sources",
                    source_ids=[source_id],
                    rationale="First rationale.",
                )
            ]
        ),
    )
    source_map_b = SourceMap(
        source_cards=[_source_card(source_id=source_id, title="Source A")],
        source_triage=SourceTriageReport(
            source_clusters=[
                SourceCluster(
                    cluster_id="cluster",
                    label="Mechanism sources",
                    source_ids=[source_id],
                    rationale="Changed rationale with the same counts.",
                )
            ]
        ),
    )

    signature_a = research_workspace_module._source_map_signature(source_map_a, inventory)
    signature_b = research_workspace_module._source_map_signature(source_map_b, inventory)

    assert signature_a["schema_version"] == "research_source_map_signature.v2"
    assert signature_a["source_map_hash"] != signature_b["source_map_hash"]


def test_research_job_prompt_payload_filters_sourcemap_to_current_job_refs() -> None:
    workspace = {
        "source_order": ["S01", "S02"],
        "investigation_jobs": [
            {
                "job_id": "job_1",
                "source_refs": ["S01"],
                "allow_broad_search": False,
                "status": "in_progress",
            }
        ],
        "atom_drafts": [],
    }
    summary = {
        "source_card_count": 2,
        "quote_card_count": 2,
        "gap_count": 0,
        "source_cards": [
            {"source_ref": "S01", "title": "Included", "summary": "Relevant"},
            {"source_ref": "S02", "title": "Excluded", "summary": "Other"},
        ],
        "quote_cards": [
            {"source_ref": "S01", "evidence_id": "ev1"},
            {"source_ref": "S02", "evidence_id": "ev2"},
        ],
        "clusters": [
            {"cluster_id": "c1", "label": "Included cluster", "source_refs": ["S01"]},
            {"cluster_id": "c2", "label": "Excluded cluster", "source_refs": ["S02"]},
        ],
        "relationships": [
            {"source_ref": "S01", "related_source_ref": "S02", "relationship": "contrast"},
            {"source_ref": "S02", "related_source_ref": "S03", "relationship": "duplicate"},
        ],
        "gaps": ["gap"],
    }

    payload = research_job_prompt_payload(workspace, "job_1", source_map_summary=summary)
    filtered = payload["source_map_summary"]

    assert [item["title"] for item in filtered["source_cards"]] == ["Included"]
    assert [item["evidence_id"] for item in filtered["quote_cards"]] == ["ev1"]
    assert [item["cluster_id"] for item in filtered["clusters"]] == ["c1"]
    assert [item["relationship"] for item in filtered["relationships"]] == ["contrast"]
    assert filtered["source_card_count"] == 2
    assert filtered["includes_evidence_ids"] is False
    assert filtered["filtered_for_source_refs"] == ["S01"]


def test_research_memo_from_workspace_assembles_downstream_source_ids() -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    run = SimpleNamespace(config={"user_prompt": "Topic"})
    workspace = {
        "memo_fields": {
            "title": "Memo",
            "thesis": "A concrete thesis.",
            "research_synthesis": "Mechanism and consequence findings.",
        },
        "atom_drafts": [
            {
                "atom_id": "atom_1",
                "kind": "mechanism",
                "title": "Mechanism",
                "explanation": "A concrete mechanism.",
                "source_refs": ["S01"],
                "source_ids": [source_id],
                "evidence_ids": [evidence_id],
                "story_value": "Explains the system.",
                "confidence": 0.8,
                "risk": "low",
                "source_coverage_status": "ready",
            }
        ],
    }

    memo = research_memo_from_workspace(run, workspace)  # type: ignore[arg-type]

    assert memo.title == "Memo"
    assert memo.research_synthesis == "Mechanism and consequence findings."
    assert memo.editorial_atoms[0].source_ids == [source_id]
    assert memo.editorial_atoms[0].evidence_ids == [evidence_id]
    assert memo.editorial_atoms[0].anchor_refs == [evidence_id]


def test_source_triage_unresolved_sets_are_blockers() -> None:
    workspace = {
        "source_sets": [
            {"set_id": "done", "status": "mapped", "source_refs": ["S01"]},
            {"set_id": "unfinished", "status": "needs_repair", "source_refs": ["S02"]},
            {"set_id": "not_started", "status": "planned", "source_refs": ["S03"]},
        ]
    }

    assert [item["set_id"] for item in unresolved_source_sets(workspace)] == ["unfinished", "not_started"]


def test_create_repair_sets_from_gate_targets_invalid_evidence_and_ungrounded_items() -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    inventory = SourceMap(
        source_cards=[
            _source_card(source_id=source_id_1, title="A"),
            _source_card(source_id=source_id_2, title="B"),
        ]
    )
    source_map = SourceMap(
        source_cards=[
            _source_card(source_id=source_id_1, title="A").model_copy(update={"evidence_ids": [evidence_id]}),
            _source_card(source_id=source_id_2, title="B"),
        ],
        source_triage=SourceTriageReport(
            source_relationships=[
                SourceRelationship(
                    source_id=source_id_1,
                    related_source_id=source_id_2,
                    relationship="duplicate",
                    evidence_ids=[],
                )
            ]
        ),
    )
    gate = SourceTriageGate(
        passed=False,
        summary="Source triage gate failed.",
        invalid_evidence_ids=[evidence_id],
        ungrounded_relationships=[f"{source_id_1}:duplicate:{source_id_2}"],
    )

    workspace = create_repair_sets_from_gate(
        {"source_order": ["S01", "S02"], "source_sets": []},
        gate=gate,
        inventory=inventory,
        source_map=source_map,
    )

    repair_set = workspace["source_sets"][0]
    assert repair_set["repair"] is True
    assert set(repair_set["source_refs"]) == {"S01", "S02"}
    assert any("invalid evidence" in question for question in repair_set["questions"])
    assert any("ungrounded" in question for question in repair_set["questions"])


@pytest.mark.asyncio
async def test_record_triage_observation_rejects_evidence_outside_current_set(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(id=uuid.uuid4())
    workspace = {
        "source_order": ["S01", "S02"],
        "current_set": {"set_id": "set_1", "source_refs": ["S01"]},
        "source_sets": [
            {
                "set_id": "set_1",
                "source_refs": ["S01"],
                "status": "in_progress",
                "observations": [],
                "evidence_ids": [],
            }
        ],
    }

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    class _FakeEvidenceToolset:
        async def _evidence_in_run(self, _evidence_id: str):
            return SimpleNamespace(source_id="source-outside-set")

        async def _source_ref_for_id(self, _source_id: str):
            return "S02"

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(),
        evidence_toolset=_FakeEvidenceToolset(),
        mode="set",
        current_set_id="set_1",
    )
    record_observation = next(tool for tool in tools if tool.name == "record_triage_observation")

    result = await record_observation.handler(
        {
            "set_id": "set_1",
            "source_refs": ["S01"],
            "evidence_ids": [str(uuid.uuid4())],
            "note": "Observation",
            "pending_questions": [],
        }
    )

    assert result["accepted"] is False
    assert result["error"] == "observation evidence_ids must be observed evidence from the current source set"


@pytest.mark.asyncio
async def test_list_source_capsules_paginates_under_model_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    source_cards = [_source_card(source_id=str(uuid.uuid4()), title=f"Source {idx:02d}") for idx in range(36)]
    inventory = SourceMap(source_cards=source_cards)
    workspace = {
        "source_order": [f"S{idx:02d}" for idx in range(1, 37)],
        "source_capsules": {},
        "source_sets": [],
    }
    run = SimpleNamespace(id=uuid.uuid4())

    async def _load(_db, _run_id):
        return workspace

    async def _save(_db, *, generation_run_id, workspace: dict, status=None):
        return SimpleNamespace(id=uuid.uuid4(), payload=workspace)

    class _FakeDb:
        async def flush(self):
            return None

    class _FakeEvidenceToolset:
        db = _FakeDb()

        async def _resolve_source_version(self, **_kwargs):
            return SimpleNamespace(id=uuid.uuid4(), content_hash="hash", updated_at=None, metadata_={})

        async def _source_windows(self, _version, max_windows: int = 3):
            text = " ".join(["representative source text"] * 80)
            return [
                {"locator_text": f"Section {idx}", "locator_type": "section", "snippet_text": text}
                for idx in range(max_windows)
            ]

    monkeypatch.setattr(triage_workspace_module, "load_source_triage_workspace", _load)
    monkeypatch.setattr(triage_workspace_module, "save_source_triage_workspace", _save)
    tools = build_workspace_tools(
        db=None,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        workspace=workspace,
        inventory=inventory,
        evidence_toolset=_FakeEvidenceToolset(),
        mode="planner",
    )
    list_capsules = next(tool for tool in tools if tool.name == "list_source_capsules")

    result = await list_capsules.handler({"limit": 12, "cursor": ""})

    assert result["accepted"] is True
    assert result["source_count"] == 36
    assert 1 <= result["returned_count"] <= 12
    assert result["next_cursor"]
    _model_facing_tool_output("list_source_capsules", result)
    assert "_model_output_truncated" not in json.dumps(result)


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
    assert runtime.fallback == ("v2_editorial_research", "research_planner_agent")


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


def test_v2_debug_dump_writes_local_json(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.debug_dump import dump_v2_debug

    monkeypatch.setattr(settings, "generation_v2_debug_dump_dir", str(tmp_path))
    monkeypatch.setattr(settings, "web_extractor_debug_dump_dir", "")

    dump_v2_debug(
        run_id="run-1",
        phase="v2_test_phase",
        agent_name="test_agent",
        dump_type="round-01-parsed",
        payload={"prompt": "hello", "parsed": {"ok": True}},
    )

    files = list((tmp_path / "generation_v2" / "v2_test_phase").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["prompt"] == "hello"
    assert payload["parsed"] == {"ok": True}


def test_v2_initial_step_control_starts_at_corpus_prepare() -> None:
    from app.api.generations import _initial_step_control

    assert _initial_step_control("v1")["next_node"] == "setup_show"
    assert _initial_step_control("v2")["next_node"] == "corpus_prepare"


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
async def test_citation_validator_blocks_unselected_evidence() -> None:
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
                locator="provider snippet",
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
                metadata={"usage_status": "observed"},
            )
        ],
        run_view=SimpleNamespace(
            selected_source_ids=[source_id],
            selected_source_version_ids=[source_version_id],
            generation_run_id=uuid.uuid4(),
        ),
    )

    assert report.passed is False
    assert "not explicitly selected" in " ".join(issue.issue for issue in report.issues)


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
        name="search_corpus",
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
    openai_read_version = SourceVersion(kind=SourceRepresentationKind.MODEL_READ_TEXT, storage_path="snapshots/openai_url_read.md", **base)

    assert _version_has_snapshot(url_version) is False
    assert _version_has_snapshot(file_version) is False
    assert _version_has_snapshot(html_version) is True
    assert _version_has_snapshot(openai_read_version) is True


def test_agent_facing_provider_versions_select_one_native_canonical_version() -> None:
    source_id = uuid.uuid4()
    base = {
        "source_id": source_id,
        "project_id": uuid.uuid4(),
        "content_hash": "hash",
        "metadata_": {"source_type": "pdf"},
        "status": "ready",
        "upload_status": "pending",
        "is_active": True,
    }
    original = SourceVersion(
        id=uuid.uuid4(),
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/report.pdf",
        **base,
    )
    extracted = SourceVersion(
        id=uuid.uuid4(),
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="snapshots/extracted.txt",
        **base,
    )

    selected = _agent_facing_provider_versions([original, extracted])

    assert selected == [original]
    assert _canonical_provider_version([original, extracted]) == original
    assert _provider_attributes(original)["agent_facing"] is True
    assert _provider_attributes(original)["canonical_retrieval_surface"] is True
    assert _provider_attributes(original)["retrieval_surface"] == "canonical"


def test_agent_facing_provider_versions_select_extracted_web_text() -> None:
    source_id = uuid.uuid4()
    base = {
        "source_id": source_id,
        "project_id": uuid.uuid4(),
        "content_hash": "hash",
        "metadata_": {"source_type": "web"},
        "status": "ready",
        "upload_status": "pending",
        "is_active": True,
    }
    fetched = SourceVersion(
        id=uuid.uuid4(),
        kind=SourceRepresentationKind.FETCHED_HTML,
        storage_path="snapshots/fetched.html",
        **base,
    )
    rendered = SourceVersion(
        id=uuid.uuid4(),
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="snapshots/rendered.txt",
        **base,
    )
    extracted = SourceVersion(
        id=uuid.uuid4(),
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="snapshots/extracted.txt",
        **base,
    )

    selected = _agent_facing_provider_versions([fetched, rendered, extracted])

    assert selected == [extracted]


def test_agent_facing_provider_versions_select_transcript_for_audio_video() -> None:
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=SourceRepresentationKind.TRANSCRIPT,
        storage_path="snapshots/transcript.txt",
        content_hash="hash",
        metadata_={"source_type": "audio"},
        status="ready",
        upload_status="pending",
        is_active=True,
    )
    page_marked = SourceVersion(
        id=uuid.uuid4(),
        source_id=version.source_id,
        project_id=version.project_id,
        kind=SourceRepresentationKind.PAGE_MARKED_TEXT,
        storage_path="snapshots/page_marked.txt",
        content_hash="hash-page",
        metadata_={"source_type": "audio"},
        status="ready",
        upload_status="pending",
        is_active=True,
    )

    assert _agent_facing_provider_versions([page_marked, version]) == [version]


def test_agent_facing_provider_versions_skip_raw_html_without_canonical_version() -> None:
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=SourceRepresentationKind.FETCHED_HTML,
        storage_path="snapshots/fetched.html",
        content_hash="hash",
        metadata_={"source_type": "web"},
        status="ready",
        upload_status="pending",
        is_active=True,
    )

    assert _agent_facing_provider_versions([version]) == []


class _FakeCorpusResult:
    def __init__(self, items: list):
        self.items = items

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        if not self.items:
            return None
        if len(self.items) > 1:
            raise AssertionError("Expected at most one fake DB result")
        return self.items[0]

    def __iter__(self):
        return iter(self.items)


class _FakeCorpusDb:
    def __init__(
        self,
        *,
        sources: list[Source],
        versions: list[SourceVersion],
        provider_index: ProviderCorpusIndex | None = None,
        provider_files: list[ProviderCorpusFile] | None = None,
    ):
        self.sources = sources
        self.versions = versions
        self.provider_index = provider_index
        self.provider_files = provider_files or []
        self.run_views: list[RunCorpusView] = []
        self.commits = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is Source:
            return _FakeCorpusResult(self.sources)
        if entity is SourceVersion:
            return _FakeCorpusResult(self.versions)
        if entity is ProviderCorpusIndex:
            return _FakeCorpusResult([self.provider_index] if self.provider_index else [])
        if entity is ProviderCorpusFile:
            return _FakeCorpusResult(self.provider_files)
        if entity is RunCorpusView:
            return _FakeCorpusResult(self.run_views)
        return _FakeCorpusResult([])

    def add(self, item):
        if isinstance(item, RunCorpusView):
            self.run_views.append(item)
        if isinstance(item, ProviderCorpusFile):
            self.provider_files.append(item)

    async def flush(self):
        for item in [*self.run_views, *self.provider_files]:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()
        return None

    async def commit(self):
        self.commits += 1


class _SequentialSourceVersionDb:
    def __init__(self, results: list[list[SourceVersion]]):
        self.results = list(results)
        self.added: list[SourceVersion] = []
        self.flush_count = 0
        self.commits = 0

    async def execute(self, _statement):
        if not self.results:
            return _FakeCorpusResult([])
        return _FakeCorpusResult(self.results.pop(0))

    def add(self, item):
        if isinstance(item, SourceVersion):
            self.added.append(item)

    async def flush(self):
        self.flush_count += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1


def _source(project_id: uuid.UUID, source_type: SourceType = SourceType.WEB) -> Source:
    return Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=source_type,
        ingestion_status="completed",
        is_temporary=False,
    )


def _source_version(
    source: Source,
    kind: SourceRepresentationKind,
    *,
    version_id: uuid.UUID | None = None,
    storage_path: str = "sources/test.txt",
) -> SourceVersion:
    return SourceVersion(
        id=version_id or uuid.uuid4(),
        source_id=source.id,
        project_id=source.project_id,
        kind=kind,
        storage_path=storage_path,
        content_hash=f"hash-{kind}",
        metadata_={"source_type": str(source.source_type)},
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        is_active=True,
    )


class _FakeProviderFiles:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.created_ids: list[str] = []

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        file_id = f"file_fake_{len(self.create_calls)}"
        self.created_ids.append(file_id)
        return SimpleNamespace(id=file_id)


class _FakeVectorStoreFiles:
    def __init__(self, *, files: dict[str, SimpleNamespace] | None = None, create_failure: Exception | None = None) -> None:
        self.files = files or {}
        self.create_failure = create_failure
        self.create_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    def list(self, **_kwargs):
        return list(self.files.values())

    async def retrieve(self, *, file_id: str, vector_store_id: str):
        item = self.files.get(file_id)
        if item is None:
            raise RuntimeError("not found")
        return item

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_failure is not None:
            raise self.create_failure
        item = SimpleNamespace(
            id=kwargs["file_id"],
            status="completed",
            attributes=kwargs.get("attributes") or {},
            last_error=None,
        )
        self.files[item.id] = item
        return item

    async def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        self.files.pop(kwargs["file_id"], None)
        return SimpleNamespace(id=kwargs["file_id"], deleted=True)


class _FakeProviderVectorStores:
    def __init__(self, files: _FakeVectorStoreFiles) -> None:
        self.files = files


class _FakeProviderClient:
    def __init__(self, vector_files: _FakeVectorStoreFiles) -> None:
        self.files = _FakeProviderFiles()
        self.vector_stores = _FakeProviderVectorStores(vector_files)


@pytest.mark.asyncio
async def test_prepare_v2_corpus_is_read_only_and_skips_not_ready_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("prepare_v2_corpus must not mutate corpus state")

    async def noop_save_tool_trace(*_args, **_kwargs):
        return None

    monkeypatch.setattr(source_corpus_module, "ensure_source_versions", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "ensure_project_provider_index", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "sync_provider_corpus_files", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "_ensure_web_snapshot_versions", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "save_tool_trace", noop_save_tool_trace)
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_enable_provider_uploads", True)

    project_id = uuid.uuid4()
    source_ready = _source(project_id)
    source_skipped = _source(project_id)
    extracted = _source_version(source_ready, SourceRepresentationKind.EXTRACTED_TEXT, storage_path="sources/ready.txt")
    rendered = _source_version(source_ready, SourceRepresentationKind.RENDERED_SNAPSHOT, storage_path="sources/rendered.txt")
    skipped_extracted = _source_version(source_skipped, SourceRepresentationKind.EXTRACTED_TEXT)
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_id=source_ready.id,
        source_version_id=extracted.id,
        provider="openai",
        provider_file_id="file_ready",
        provider_vector_store_file_id="vsf_ready",
        attributes={"is_active": True, "agent_facing": True, "canonical_retrieval_surface": True},
        status=V2RecordStatus.READY,
    )
    fake_db = _FakeCorpusDb(
        sources=[source_ready, source_skipped],
        versions=[extracted, rendered, skipped_extracted],
        provider_index=provider_index,
        provider_files=[provider_file],
    )
    run = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, config={})

    result = await prepare_v2_corpus(fake_db, run)  # type: ignore[arg-type]

    assert [str(item.id) for item in result.sources] == [str(source_ready.id)]
    assert set(result.run_view.selected_source_version_ids) == {str(extracted.id), str(rendered.id)}
    assert result.run_view.active_source_filter["canonical_source_version_ids"] == [str(extracted.id)]
    assert result.run_view.active_source_filter["fallback_source_version_ids"] == [str(rendered.id)]
    assert result.run_view.eligible_provider_file_ids == ["file_ready"]
    assert result.run_view.readiness_summary["skipped_source_count"] == 1
    assert result.run_view.readiness_summary["skipped_sources"][0]["reason"] == "canonical_provider_file_not_ready"


@pytest.mark.asyncio
async def test_prepare_v2_corpus_fails_when_every_source_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    async def noop_save_tool_trace(*_args, **_kwargs):
        return None

    monkeypatch.setattr(source_corpus_module, "save_tool_trace", noop_save_tool_trace)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "generation_v2_enable_provider_uploads", True)

    project_id = uuid.uuid4()
    source = _source(project_id)
    fetched = _source_version(source, SourceRepresentationKind.FETCHED_HTML, storage_path="sources/fetched.html")
    fake_db = _FakeCorpusDb(sources=[source], versions=[fetched])
    run = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, config={})

    with pytest.raises(RuntimeError, match="no usable sources"):
        await prepare_v2_corpus(fake_db, run)  # type: ignore[arg-type]

    assert fake_db.run_views[0].status == V2RecordStatus.FAILED
    assert fake_db.run_views[0].readiness_summary["skipped_sources"][0]["reason"] == "no_canonical_source_version"


@pytest.mark.asyncio
async def test_project_corpus_index_sync_is_provider_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("corpus-index sync must not create source versions or render/fetch sources")

    project_id = uuid.uuid4()
    source = _source(project_id)
    version = _source_version(source, SourceRepresentationKind.EXTRACTED_TEXT)
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    fake_db = _FakeCorpusDb(sources=[source], versions=[version], provider_index=provider_index)
    captured: dict[str, object] = {}

    async def fake_ensure_provider_index(db, requested_project_id, **_kwargs):
        assert db is fake_db
        assert requested_project_id == project_id
        return provider_index

    async def fake_sync_provider_files(db, run, requested_provider_index, versions, *, source_ids=None):
        assert db is fake_db
        assert run is None
        assert requested_provider_index is provider_index
        captured["versions"] = versions
        captured["source_ids"] = source_ids
        return []

    monkeypatch.setattr(source_corpus_module, "ensure_source_versions", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "_ensure_web_snapshot_versions", fail_if_called)
    monkeypatch.setattr(source_corpus_module, "ensure_project_provider_index", fake_ensure_provider_index)
    monkeypatch.setattr(source_corpus_module, "sync_provider_corpus_files", fake_sync_provider_files)

    summary = await reconcile_project_corpus_index_now(fake_db, project_id)  # type: ignore[arg-type]

    assert captured["versions"] == [version]
    assert captured["source_ids"] == {source.id}
    assert summary == {"source_count": 1, "source_version_count": 1, "provider_file_count": 0}


@pytest.mark.asyncio
async def test_ensure_source_version_replaces_after_new_row_without_provider_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("replacement must not detach provider files before replacement provider is ready")

    monkeypatch.setattr(source_corpus_module, "mark_provider_files_inactive", fail_if_called)

    project_id = uuid.uuid4()
    source = _source(project_id)
    previous = _source_version(
        source,
        SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="snapshots/old-rendered.txt",
    )
    fake_db = _SequentialSourceVersionDb(results=[[], [previous]])

    replacement = await _ensure_source_version(
        fake_db,  # type: ignore[arg-type]
        source,
        kind=SourceRepresentationKind.RENDERED_SNAPSHOT,
        storage_path="snapshots/new-rendered.txt",
        content_hash="hash-new-rendered",
        metadata={"canonical": True},
    )

    assert replacement in fake_db.added
    assert replacement.is_active is True
    assert replacement.supersedes_source_version_id == previous.id
    assert previous.is_active is False
    assert fake_db.commits == 0


@pytest.mark.asyncio
async def test_provider_upload_resume_reuses_file_created_before_attach_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_enable_provider_uploads", True)
    monkeypatch.setattr(source_corpus_module, "download_text", lambda _client, _path: "source text")

    project_id = uuid.uuid4()
    source = _source(project_id)
    version = _source_version(source, SourceRepresentationKind.EXTRACTED_TEXT)
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    fake_db = _FakeCorpusDb(sources=[source], versions=[version], provider_index=provider_index)
    first_vector_files = _FakeVectorStoreFiles(create_failure=RuntimeError("attach unavailable"))
    first_client = _FakeProviderClient(first_vector_files)
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: first_client)

    first_result = await _ensure_provider_file(fake_db, None, provider_index, version)  # type: ignore[arg-type]

    assert first_client.files.create_calls
    assert first_result.provider_file_id == "file_fake_1"
    assert first_result.status == V2RecordStatus.FAILED
    assert fake_db.commits >= 2

    completed_vector_file = SimpleNamespace(
        id="file_fake_1",
        status="completed",
        attributes=_provider_attributes(version),
        last_error=None,
    )
    second_vector_files = _FakeVectorStoreFiles(files={"file_fake_1": completed_vector_file})
    second_client = _FakeProviderClient(second_vector_files)
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: second_client)

    resumed = await _ensure_provider_file(fake_db, None, provider_index, version)  # type: ignore[arg-type]

    assert second_client.files.create_calls == []
    assert second_vector_files.create_calls == []
    assert resumed.provider_file_id == "file_fake_1"
    assert resumed.status == V2RecordStatus.READY
    assert version.upload_status == V2RecordStatus.READY


@pytest.mark.asyncio
async def test_reconcile_adopts_completed_openai_file_for_existing_canonical_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(project_id)
    version = _source_version(source, SourceRepresentationKind.EXTRACTED_TEXT)
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    vector_file = SimpleNamespace(
        id="file_adopt",
        status="completed",
        attributes=_provider_attributes(version),
        last_error=None,
    )
    vector_files = _FakeVectorStoreFiles(files={"file_adopt": vector_file})
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: _FakeProviderClient(vector_files))
    fake_db = _FakeCorpusDb(sources=[source], versions=[version], provider_index=provider_index)

    summary = await reconcile_provider_corpus_files(
        fake_db,  # type: ignore[arg-type]
        provider_index,
        [version],
        source_ids={source.id},
    )

    assert summary["adopted_count"] == 1
    assert len(fake_db.provider_files) == 1
    provider_file = fake_db.provider_files[0]
    assert provider_file.provider_file_id == "file_adopt"
    assert provider_file.status == V2RecordStatus.READY
    assert provider_file.attributes["upload_phase"] == "adopted"
    assert version.provider_file_id == "file_adopt"
    assert version.upload_status == V2RecordStatus.READY


@pytest.mark.asyncio
async def test_reconcile_retains_db_backed_inactive_provider_file_until_replacement_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(project_id)
    version = _source_version(source, SourceRepresentationKind.RENDERED_SNAPSHOT)
    version.is_active = False
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    provider_file = ProviderCorpusFile(
        id=uuid.uuid4(),
        provider_index_id=provider_index.id,
        source_version_id=version.id,
        source_id=source.id,
        provider="openai",
        provider_file_id="file_stale_but_db_backed",
        provider_vector_store_file_id="file_stale_but_db_backed",
        status=V2RecordStatus.READY,
        attributes=_provider_attributes(version),
    )
    vector_file = SimpleNamespace(
        id="file_stale_but_db_backed",
        status="completed",
        attributes=_provider_attributes(version),
        last_error=None,
    )
    vector_files = _FakeVectorStoreFiles(files={"file_stale_but_db_backed": vector_file})
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: _FakeProviderClient(vector_files))
    fake_db = _FakeCorpusDb(
        sources=[source],
        versions=[version],
        provider_index=provider_index,
        provider_files=[provider_file],
    )

    summary = await reconcile_provider_corpus_files(
        fake_db,  # type: ignore[arg-type]
        provider_index,
        [version],
        source_ids={source.id},
    )

    assert summary["retained_stale_db_file_count"] == 1
    assert summary["invalid_orphan_count"] == 0
    assert summary["detached_count"] == 0
    assert vector_files.delete_calls == []
    assert fake_db.provider_files == [provider_file]


@pytest.mark.asyncio
async def test_reconcile_detaches_openai_file_with_missing_source_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    project_id = uuid.uuid4()
    source = _source(project_id)
    version = _source_version(source, SourceRepresentationKind.EXTRACTED_TEXT)
    provider_index = ProviderCorpusIndex(
        id=uuid.uuid4(),
        project_id=project_id,
        provider="openai",
        provider_vector_store_id="vs_test",
        scope="project",
        status=V2RecordStatus.READY,
        config={},
    )
    orphan_attributes = {
        **_provider_attributes(version),
        "source_version_id": str(uuid.uuid4()),
    }
    vector_file = SimpleNamespace(id="file_orphan", status="completed", attributes=orphan_attributes, last_error=None)
    vector_files = _FakeVectorStoreFiles(files={"file_orphan": vector_file})
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: _FakeProviderClient(vector_files))
    fake_db = _FakeCorpusDb(sources=[source], versions=[version], provider_index=provider_index)

    summary = await reconcile_provider_corpus_files(
        fake_db,  # type: ignore[arg-type]
        provider_index,
        [version],
        source_ids={source.id},
    )

    assert summary["invalid_orphan_count"] == 1
    assert summary["detached_count"] == 1
    assert vector_files.delete_calls[0]["file_id"] == "file_orphan"
    assert fake_db.provider_files == []


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


def test_original_pdf_provider_upload_payload_normalizes_arxiv_filename(monkeypatch) -> None:
    payload = b"%PDF-1.7\nbinary-ish bytes"

    def fake_download_file(_client, path):
        assert path == "sources/project/2511.10715"
        return payload

    monkeypatch.setattr("app.generation_v2.source_corpus.download_file", fake_download_file)
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=SourceRepresentationKind.ORIGINAL_FILE,
        storage_path="sources/project/2511.10715",
        content_hash=hashlib.sha256(payload).hexdigest(),
        metadata_={"original_filename": "2511.10715", "source_type": "pdf"},
        status="ready",
        upload_status="pending",
        is_active=True,
    )

    filename, content = _version_upload_payload(version)

    assert filename == "2511.10715.pdf"
    assert content == payload


def test_pdf_url_response_is_detected_for_native_openai_ingest() -> None:
    assert _is_pdf_response("https://example.com/report.pdf", "text/plain", b"not really pdf") is False
    assert _is_pdf_response("https://example.com/report.pdf", "text/html", b"<html></html>") is False
    assert _is_pdf_response("https://example.com/report.pdf", "", b"") is True
    assert _is_pdf_response("https://example.com/download", "application/pdf", b"opaque") is True
    assert _is_pdf_response("https://example.com/download", "application/octet-stream", b"%PDF-1.7\n") is True
    assert _is_pdf_response("https://example.com/article", "text/html", b"<html></html>") is False


@pytest.mark.asyncio
async def test_openai_url_read_uses_responses_file_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_corpus as source_corpus_module

    fake_client = _FakeModelReadClient(
        {
            "read_status": "readable",
            "title": "Readable article",
            "canonical_url": "https://example.com/article.html",
            "content_type": "text/html",
            "extracted_text_markdown": "# Readable article\n\nBody text.",
            "summary": "The article is readable.",
            "failure_reason": "",
            "confidence": 0.9,
        }
    )
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "generation_v2_corpus_prepare_model", "gpt-5.4-mini")
    monkeypatch.setattr(source_corpus_module, "get_openai_client", lambda: fake_client)

    result = await _call_openai_url_file_read("https://example.com/article.html")

    call = fake_client.responses.calls[0]
    assert call["model"] == "gpt-5.4-mini"
    assert call["input"][0]["content"][1] == {
        "type": "input_file",
        "file_url": "https://example.com/article.html",
    }
    assert call["text"]["format"]["name"] == "OpenAIUrlReadResult"
    assert result["read_status"] == "readable"
    assert result["_response_id"] == "resp_model_read"
    assert result["_requested_model"] == "gpt-5.4-mini"


def test_filename_from_url_keeps_provider_upload_name() -> None:
    assert _filename_from_url("https://example.com/path/Report%202025.pdf?download=1", default="fallback.pdf") == "Report 2025.pdf"
    assert _filename_from_url("https://example.com/path/", default="fallback.pdf") == "fallback.pdf"
    assert (
        _filename_from_url("https://arxiv.org/pdf/2511.10715", default="fallback.pdf", required_suffix=".pdf")
        == "2511.10715.pdf"
    )


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
    kwargs_by_agent: dict[str, dict] = {}

    def __init__(self, response):
        self.response = response

    async def call_structured(self, **kwargs):
        self.calls[kwargs["agent_name"]] = [tool.name for tool in kwargs.get("local_tools") or []]
        self.kwargs_by_agent[kwargs["agent_name"]] = kwargs
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


class _FakeWriteActionDb:
    def __init__(self, *, traces: list[SimpleNamespace], evidence: list[SimpleNamespace]) -> None:
        self._responses = [_FakeScalarResult(traces), _FakeScalarResult(evidence)]

    async def execute(self, _query):
        return self._responses.pop(0)


class _FakeModelReadDb:
    def __init__(
        self,
        *,
        provider_files: list[ProviderCorpusFile],
        versions: dict[uuid.UUID, SourceVersion],
        execute_values: list[list[object]] | None = None,
    ) -> None:
        self._responses = [_FakeScalarResult(values) for values in (execute_values if execute_values is not None else [provider_files, []])]
        self._provider_files = provider_files
        self._versions = versions
        self.added: list[object] = []

    async def execute(self, _query):
        query_text = str(_query).lower()
        if "evidence_items" in query_text:
            return _FakeScalarResult([item for item in self.added if isinstance(item, EvidenceItem)])
        if "provider_corpus_files" in query_text and self._provider_files:
            return _FakeScalarResult(self._provider_files)
        if self._responses:
            return self._responses.pop(0)
        return _FakeScalarResult([])

    async def get(self, model, item_id):
        if model is SourceVersion:
            return self._versions.get(item_id)
        return None

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if isinstance(item, EvidenceItem) and item.id is None:
                item.id = uuid.uuid4()
            if isinstance(item, V2ToolTrace) and item.id is None:
                item.id = uuid.uuid4()


class _FakeModelReadResponses:
    def __init__(self, payload: dict, *, failures: int = 0) -> None:
        self.payload = payload
        self.failures = failures
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise RuntimeError("input_file too large")
        return SimpleNamespace(id="resp_model_read", output_text=json.dumps(self.payload))


class _FakeModelReadFiles:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        suffix = "" if len(self.calls) == 1 else f"_{len(self.calls)}"
        return SimpleNamespace(id=f"file_model_read{suffix}")


class _FakeModelReadClient:
    def __init__(self, payload: dict, *, response_failures: int = 0) -> None:
        self.files = _FakeModelReadFiles()
        self.responses = _FakeModelReadResponses(payload, failures=response_failures)


class _FakeSelectEvidenceDb:
    def __init__(self, evidence: EvidenceItem) -> None:
        self.evidence = evidence

    async def get(self, model, item_id):
        if model is EvidenceItem and item_id == self.evidence.id:
            return self.evidence
        return None

    async def flush(self):
        return None


class _FakeScalarResult:
    def __init__(self, values: list[SimpleNamespace]) -> None:
        self._values = values

    def scalars(self):
        return self._values

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


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
            "search_corpus",
            "find_quotes",
            "read_evidence",
            "read_source_window",
            "read_source_with_model",
            "skim_source",
            "inspect_source",
            "search_within_source",
            "compare_source_evidence",
            "compare_evidence_for_question",
            "select_evidence",
            "upsert_source_card",
            "create_quote_card",
            "record_source_relationship",
            "record_source_cluster",
            "record_gap",
            "finalize_source_map",
        )
    ]


def _workspace_triage_tools() -> list[AgentTool]:
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
            "list_source_capsules",
            "upsert_triage_source_set",
            "finalize_triage_workspace_plan",
            "record_triage_observation",
            "finish_triage_source_set",
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
    metadata: dict | None = None,
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
        "metadata": metadata or {"usage_status": "selected"},
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


def test_source_acquisition_prompt_keeps_web_search_as_discovery_only() -> None:
    assert "web_search for discovery only" in SOURCE_ACQUISITION_PROMPT
    assert "Call ingest_source_candidate" in SOURCE_ACQUISITION_PROMPT
    assert "Final support must come later from ingested project sources" in SOURCE_ACQUISITION_PROMPT
    assert "Return only SourceAcquisitionCompletion JSON" in SOURCE_ACQUISITION_PROMPT


def test_source_acquisition_toolset_exposes_only_acquisition_write_tools() -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    gap_report = CorpusGapReport(
        run_id=str(run.id),
        project_id=str(run.project_id),
        trigger_phase="source_triage",
        target_duration_seconds=3600,
        summary="Need sources",
        gaps=[
            {
                "gap_id": "gap-1",
                "kind": "missing_function",
                "description": "Need mechanism coverage",
                "required_editorial_functions": ["mechanism"],
            }
        ],
    )
    toolset = SourceAcquisitionToolset(db=SimpleNamespace(), run=run, gap_report=gap_report, round_number=1)  # type: ignore[arg-type]

    names = {tool.name for tool in toolset.agent_tools()}

    assert names == {"ingest_source_candidate", "reject_source_candidate", "finalize_source_acquisition_round"}
    assert all("evidence" not in name for name in names)
    assert all("source_map" not in name for name in names)


@pytest.mark.asyncio
async def test_source_acquisition_agent_uses_web_search_and_acquisition_tools_only() -> None:
    captured = {}

    class Runtime:
        async def call_structured(self, **kwargs):
            captured.update(kwargs)
            return kwargs["schema_model"](
                ready=True,
                summary="done",
                searched_gap_ids=["gap-1"],
                accepted_urls=[],
                rejected_urls=[],
                notes=[],
            )

    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        config={"user_prompt": "topic", "duration_target": 3600, "language": "en"},
    )
    gap_report = CorpusGapReport(
        run_id=str(run.id),
        project_id=str(run.project_id),
        trigger_phase="source_triage",
        target_duration_seconds=3600,
        summary="Need sources",
        gaps=[
            {
                "gap_id": "gap-1",
                "kind": "missing_function",
                "description": "Need mechanism coverage",
            }
        ],
    )
    async def _handler(_args):
        return {}

    tools = [
        AgentTool(name="ingest_source_candidate", description="", parameters={"type": "object", "properties": {}}, handler=_handler),
        AgentTool(name="reject_source_candidate", description="", parameters={"type": "object", "properties": {}}, handler=_handler),
        AgentTool(name="finalize_source_acquisition_round", description="", parameters={"type": "object", "properties": {}}, handler=_handler),
        AgentTool(name="search_corpus", description="", parameters={"type": "object", "properties": {}}, handler=_handler),
    ]

    await build_source_acquisition_agent(
        SimpleNamespace(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        Runtime(),  # type: ignore[arg-type]
        gap_report=gap_report,
        source_inventory=SourceMap(),
        acquisition_tools=tools,
        round_number=1,
    )

    assert captured["phase"] == "v2_source_acquisition"
    assert captured["tool_policy"].web_search is True
    assert captured["tool_policy"].file_search is False
    assert {tool.name for tool in captured["local_tools"]} == {
        "ingest_source_candidate",
        "reject_source_candidate",
        "finalize_source_acquisition_round",
    }


def test_corpus_gap_report_collects_triage_gate_failures() -> None:
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"duration_target": 5400})
    source_map = SourceMap(
        source_cards=[_source_card(source_id="src-1")],
        evidence_ids=["ev-1"],
        gaps=["Missing human cost reporting."],
    )
    gate = SourceTriageGate(
        passed=False,
        summary="failed",
        missing_function_evidence=["mechanism"],
        missing_source_card_evidence_source_ids=["src-2"],
    )

    report = build_corpus_gap_report(
        run,  # type: ignore[arg-type]
        trigger_phase="source_triage",
        round_number=1,
        source_map=source_map,
        triage_ready=False,
        triage_completion_summary="not enough coverage",
        triage_gate=gate,
    )

    assert report.should_acquire is True
    assert report.source_count == 1
    assert report.evidence_count == 1
    assert any(gap.kind == "triage_not_ready" for gap in report.gaps)
    assert any(gap.kind == "missing_function" and "mechanism" in gap.required_editorial_functions for gap in report.gaps)
    assert any(gap.kind == "source_map_gap" for gap in report.gaps)


def test_source_acquisition_candidate_validation_rejects_bad_urls_and_unknown_gaps(monkeypatch) -> None:
    monkeypatch.setattr(settings, "generation_v2_source_acquisition_min_confidence", 0.55)
    report = CorpusGapReport(
        run_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        trigger_phase="source_triage",
        target_duration_seconds=3600,
        summary="Need sources",
        gaps=[
            {
                "gap_id": "gap-1",
                "kind": "missing_function",
                "description": "Need mechanism coverage",
            }
        ],
    )

    search_candidate = SourceAcquisitionCandidate(
        url="https://www.google.com/search?q=topic",
        normalized_url=normalize_candidate_url("https://www.google.com/search?q=topic"),
        gap_ids=["gap-1"],
        rationale="Search page",
        confidence=0.9,
    )
    unknown_gap_candidate = SourceAcquisitionCandidate(
        url="https://example.com/report",
        normalized_url=normalize_candidate_url("https://example.com/report"),
        gap_ids=["missing-gap"],
        rationale="Looks useful",
        confidence=0.9,
    )
    low_confidence_candidate = SourceAcquisitionCandidate(
        url="https://example.com/report",
        normalized_url=normalize_candidate_url("https://example.com/report"),
        gap_ids=["gap-1"],
        rationale="Weak",
        confidence=0.2,
    )
    good_candidate = SourceAcquisitionCandidate(
        url="https://example.com/report?utm_source=x&b=1",
        normalized_url=normalize_candidate_url("https://example.com/report?utm_source=x&b=1"),
        gap_ids=["gap-1"],
        rationale="Primary-looking report",
        confidence=0.8,
    )

    assert "Search result pages" in (validate_candidate_static(search_candidate, report) or "")
    assert "known gap_ids" in (validate_candidate_static(unknown_gap_candidate, report) or "")
    assert "below" in (validate_candidate_static(low_confidence_candidate, report) or "")
    assert validate_candidate_static(good_candidate, report) is None
    assert good_candidate.normalized_url == "https://example.com/report?b=1"
