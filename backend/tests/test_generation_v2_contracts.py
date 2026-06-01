from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.editorial import (
    _fallback_act_draft,
    _fallback_claims,
    _fallback_research_memo,
    _fallback_treatment,
    build_act_dossier,
    build_research_memo,
    update_continuity_ledger,
)
from app.generation_v2.runtime import V2RuntimeUnavailable, _function_calls, _strict_json_schema
from app.generation_v2.schemas import ContinuityLedger, FactIssue, QuoteCard, ResearchMemo, SourceCard
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


@pytest.mark.asyncio
async def test_v2_agent_falls_back_only_when_runtime_unavailable() -> None:
    class RuntimeUnavailable:
        async def call_structured(self, **kwargs):
            raise V2RuntimeUnavailable("no key")

    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), config={"user_prompt": "Topic"})
    source_cards = [
        SourceCard(source_id=str(uuid.uuid4()), title="Source", source_type="pdf", summary="Specific source text.")
    ]

    memo = await build_research_memo(
        None,
        run,
        RuntimeUnavailable(),
        source_cards=source_cards,
        quote_cards=[],
        vector_store_id=None,
    )

    assert memo.risk_register == ["Fallback memo used because provider runtime was unavailable."]


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
