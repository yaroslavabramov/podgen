from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.editorial_context import source_inventory as editorial_source_inventory
from app.generation_v2.prompting.source import (
    CORPUS_BRIEF_SHADOW_AUDIT_PROMPT,
    SOURCE_TRIAGE_FINALIZER_PROMPT,
    SOURCE_TRIAGE_SET_PROMPT,
)
from app.generation_v2.prompting.system import CORPUS_BRIEF_AUDIT_SYSTEM
from app.generation_v2.prompting.system import BASE_PODCAST_SYSTEM
from app.generation_v2.research_source_map_prompting import source_map_research_page
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_canonical import materialize_source_map_v2
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_COMMIT_TOOLS
from app.generation_v2.source_triage_workspace_tools import _full_canonical_item_page
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus
from app.models.source import Source, SourceType


def _assessment(
    source_ref: str,
    source_id: str,
    *,
    representation_status: str = "readable",
    relevance: str = "core",
    disposition: str = "anchor",
) -> dict:
    return {
        "assessment_id": f"assessment:{source_id}",
        "set_id": "set-main",
        "source_ref": source_ref,
        "source_id": source_id,
        "source_version_ids": [str(uuid.uuid4())],
        "source_profile_id": str(uuid.uuid4()),
        "processing_status": "resolved",
        "representation_status": representation_status,
        "representation_reason": f"Representation is {representation_status}.",
        "relevance": relevance,
        "relevance_reason": "Assessed against the requested topic.",
        "authority": "official",
        "authority_reason": "Official publisher.",
        "uniqueness": "unique",
        "uniqueness_reason": "Distinct evidence.",
        "disposition": disposition,
        "coverage_question_ids": ["q-main"],
        "editorial_functions": ["mechanism"],
        "evidence_ids": [f"evidence:{source_ref}"],
        "limitations": [],
        "confidence": 0.8,
    }


def _card(source_id: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[str(uuid.uuid4())],
        title=f"Source {source_id}",
        source_type="web",
        summary="Neutral source summary.",
    )


def test_canonical_triage_pages_expose_refs_not_internal_lineage_ids() -> None:
    source_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    profile_id = str(uuid.uuid4())
    assessment_id = f"assessment:{source_id}"
    workspace = {
        "coverage_questions": [
            {
                "question_id": "q-main",
                "question": "What changed?",
                "kind": "change",
                "importance": "critical",
                "rationale": "Needed for orientation.",
            }
        ],
        "source_assessments": [
            {
                **_assessment("S01", source_id),
                "assessment_id": assessment_id,
                "source_version_ids": [version_id],
                "source_profile_id": profile_id,
            }
        ],
        "coverage_outcomes": [
            {
                "outcome_id": f"outcome:{source_id}",
                "set_id": "set-main",
                "question_id": "q-main",
                "status": "covered",
                "source_ids": [source_id],
                "evidence_ids": ["evidence-1"],
                "orientation_summary": "The source establishes the change.",
                "next_action": "research",
            }
        ],
    }

    assessment_page = _full_canonical_item_page(
        workspace,
        section="source_assessments",
        cursor=0,
    )
    outcome_page = _full_canonical_item_page(
        workspace,
        section="coverage_outcomes",
        cursor=0,
    )

    assessment = assessment_page["items"][0]
    outcome = outcome_page["items"][0]
    encoded = json.dumps({"assessment": assessment, "outcome": outcome})
    assert assessment["assessment_ref"] == "assessment:S01"
    assert assessment["source_ref"] == "S01"
    assert outcome["source_refs"] == ["S01"]
    assert outcome["outcome_ref"] == "outcome:q-main:set-main"
    assert "source_id" not in assessment
    assert "source_version_ids" not in assessment
    assert "source_profile_id" not in assessment
    assert "source_ids" not in outcome
    assert source_id not in encoded
    assert version_id not in encoded
    assert profile_id not in encoded


def test_ranked_sources_include_only_research_eligible_assessments() -> None:
    assessments = [
        _assessment("S01", "source-unreadable", representation_status="unreadable"),
        _assessment("S02", "source-eligible"),
        _assessment("S03", "source-unusable", disposition="unusable"),
        _assessment("S04", "source-deferred", disposition="deferred"),
        _assessment("S05", "source-out-of-scope", relevance="out_of_scope"),
        _assessment("S06", "source-workspace-deferred"),
        _assessment(
            "S07",
            "source-not-target-content",
            representation_status="not_target_content",
            disposition="deferred",
        ),
    ]
    cards = [_card(item["source_id"]) for item in assessments]
    workspace = {
        "source_order": [item["source_ref"] for item in assessments],
        "source_assessments": assessments,
        "deferred_sources": [
            {"source_ref": "S06", "reason": "Explicitly deferred after reading."},
        ],
    }

    result = materialize_source_map_v2(
        SourceMap(source_cards=cards),
        workspace=workspace,
        inventory=SourceMap(source_cards=cards),
    )

    assert len(result.source_cards) == 7
    assert [item.source_id for item in result.source_triage.ranked_sources] == ["source-eligible"]
    assert [item.rank for item in result.source_triage.ranked_sources] == [1]
    assert result.source_triage.top_sources_by_function == {"mechanism": ["source-eligible"]}
    coverage_by_source = {item.source_id: str(item.coverage_status) for item in result.source_cards}
    assert coverage_by_source["source-not-target-content"] == "failed"


def test_new_canonical_writes_are_trace_classifications_without_read_limits() -> None:
    assert "save_source_analysis" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "save_coverage_hint" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "upsert_coverage_outcome" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "resolve_triage_question" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "update_source_finding_links" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "update_source_finding_relations" in TRIAGE_SEMANTIC_COMMIT_TOOLS
    assert "upsert_source_assessment" not in TRIAGE_SEMANTIC_COMMIT_TOOLS


def test_triage_prompts_define_completed_unreadable_sources_and_scoped_evidence() -> None:
    assert "representation failures as" in SOURCE_TRIAGE_SET_PROMPT
    assert "representation limits instead of inferring content" in SOURCE_TRIAGE_SET_PROMPT
    assert "Finish only when unfinished_work is empty" in SOURCE_TRIAGE_SET_PROMPT
    assert "Evidence attachments are scoped to the map item that cites them" in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "does not also have to appear on every related" in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "never guess, abbreviate, or synthesize an evidence_id" in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "Write generated prose fields in that language" in BASE_PODCAST_SYSTEM
    assert "schema keys, enum literals, IDs, source/evidence handles" in BASE_PODCAST_SYSTEM
    assert "reconcile a run-wide result for each semantic requirement" not in SOURCE_TRIAGE_FINALIZER_PROMPT
    assert "Do not write coverage outcomes" in SOURCE_TRIAGE_FINALIZER_PROMPT
    assert "source-specific absence" in " ".join(
        SOURCE_TRIAGE_FINALIZER_PROMPT.split()
    )


class _Result:
    def __init__(self, items: list[object]):
        self.items = items

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.items)


class _InventoryDb:
    def __init__(self, results: list[list[object]]):
        self.results = list(results)

    async def execute(self, _statement):
        return _Result(self.results.pop(0))


@pytest.mark.asyncio
async def test_source_inventory_persists_every_representation_in_full_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_inventory

    project_id = uuid.uuid4()
    source = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://example.com/source",
        title="Source with many representations",
        text_preview="Neutral preview.",
        metadata_={},
        domain_reliability="unknown",
        authority_type="unknown",
    )
    versions = [
        SourceVersion(
            id=uuid.uuid4(),
            source_id=source.id,
            project_id=project_id,
            kind=f"representation_{index:02d}",
            storage_path=f"source/{index}.txt",
            status=V2RecordStatus.READY,
            upload_status=V2RecordStatus.PENDING,
            metadata_={},
            is_active=True,
        )
        for index in range(12)
    ]
    run_view = SimpleNamespace(
        selected_source_ids=[str(source.id)],
        selected_source_version_ids=[str(version.id) for version in versions],
    )
    db = _InventoryDb([[source], versions, []])

    async def no_profile(*_args, **_kwargs):
        return None

    monkeypatch.setattr(evidence_source_inventory, "ensure_legacy_source_profile", no_profile)

    inventory = await evidence_source_inventory.build_source_inventory(db, run_view)  # type: ignore[arg-type]

    card = inventory.source_cards[0]
    assert len(card.representation_ids) == 12
    for version in versions:
        assert f"{version.kind}:{version.status}/{version.upload_status}" in card.representation_summary
    assert "representation_11" in card.representation_summary


@pytest.mark.asyncio
async def test_source_inventory_prefers_semantic_text_over_page_marker_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_inventory

    project_id = uuid.uuid4()
    source = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://example.com/source",
        title="Source",
        text_preview="Neutral preview.",
        metadata_={},
    )
    page_marked = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=project_id,
        kind=SourceRepresentationKind.PAGE_MARKED_TEXT,
        storage_path="source/page-marked.txt",
        status=V2RecordStatus.READY,
        metadata_={},
    )
    extracted = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=project_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="source/extracted.txt",
        status=V2RecordStatus.READY,
        metadata_={},
    )
    run_view = SimpleNamespace(
        selected_source_ids=[str(source.id)],
        selected_source_version_ids=[str(page_marked.id), str(extracted.id)],
    )
    db = _InventoryDb([[source], [page_marked, extracted], []])
    profiled_versions: list[SourceVersion] = []

    async def capture_profile(_db, version):
        profiled_versions.append(version)
        return None

    monkeypatch.setattr(evidence_source_inventory, "ensure_legacy_source_profile", capture_profile)

    inventory = await evidence_source_inventory.build_source_inventory(db, run_view)  # type: ignore[arg-type]

    assert inventory.source_cards[0].source_version_ids == [str(extracted.id), str(page_marked.id)]
    assert profiled_versions == [extracted]


@pytest.mark.asyncio
async def test_source_inventory_preview_is_bound_to_selected_version_not_mutable_source_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_inventory

    project_id = uuid.uuid4()
    source = Source(
        id=uuid.uuid4(),
        project_id=project_id,
        source_type=SourceType.WEB,
        original_url="https://example.com/report",
        title="Report",
        text_preview="403 ERROR. The request could not be satisfied. Generated by CloudFront.",
        metadata_={},
    )
    version = SourceVersion(
        id=uuid.uuid4(),
        source_id=source.id,
        project_id=project_id,
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="source/report.txt",
        status=V2RecordStatus.READY,
        metadata_={},
    )
    run_view = SimpleNamespace(
        selected_source_ids=[str(source.id)],
        selected_source_version_ids=[str(version.id)],
    )
    db = _InventoryDb([[source], [version], []])

    async def no_profile(*_args, **_kwargs):
        return None

    monkeypatch.setattr(evidence_source_inventory, "ensure_legacy_source_profile", no_profile)
    monkeypatch.setattr(
        evidence_source_inventory,
        "source_version_text",
        lambda _version: "The selected article explains the mechanism and its historical consequences.",
    )

    inventory = await evidence_source_inventory.build_source_inventory(db, run_view)  # type: ignore[arg-type]

    summary = inventory.source_cards[0].summary
    assert "selected article" in summary
    assert "CloudFront" not in summary


def test_model_context_uses_separate_representation_summary_preview() -> None:
    long_summary = "representation:ready/pending, " * 100
    card = SourceCard(
        source_id="source-1",
        source_version_ids=["version-1"],
        title="Source",
        source_type="web",
        summary="Neutral summary.",
        representation_summary=long_summary,
    )
    source_map = SourceMap(source_cards=[card])

    page_item = source_map_research_page(
        source_map,
        section="source_map_cards",
    )["items"][0]
    editorial_item = editorial_source_inventory([card])[0]

    # Explicit SourceMap pages are the lossless recovery path; only the compact
    # editorial inventory is allowed to expose a preview.
    assert page_item["representation_summary"] == long_summary
    assert "representation_summary_preview" not in page_item
    assert "representation_summary" not in editorial_item
    assert len(editorial_item["representation_summary_preview"]) <= 180
    assert editorial_item["representation_summary_preview"].endswith("...")
    assert editorial_item["representation_summary_chars"] == len(long_summary)
    assert editorial_item["representation_summary_truncated"] is True


def test_shadow_audit_prompt_forbids_guesses_from_omitted_preview() -> None:
    assert "Do not infer that an omitted question" in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "leave unverifiable matters unjudged" in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "important persisted question or selected source role disappeared" not in CORPUS_BRIEF_SHADOW_AUDIT_PROMPT
    assert "Never guess about omitted or truncated content" in CORPUS_BRIEF_AUDIT_SYSTEM


def test_triage_set_prompt_separates_reading_navigation_and_final_evidence() -> None:
    assert "read_source returns the actual text as lossless" in SOURCE_TRIAGE_SET_PROMPT
    assert "search_source is navigation only, not proof" in SOURCE_TRIAGE_SET_PROMPT
    assert "Before switching sources, save either a reading note" in SOURCE_TRIAGE_SET_PROMPT
    assert "passage_refs/evidence_ids" in SOURCE_TRIAGE_SET_PROMPT
    assert "Use question_index" in SOURCE_TRIAGE_SET_PROMPT
    assert "For a mechanical rejection" in SOURCE_TRIAGE_SET_PROMPT
    assert "Do not answer coverage questions" in SOURCE_TRIAGE_SET_PROMPT
    assert "coverage_hints" in SOURCE_TRIAGE_SET_PROMPT
    assert "question_id values, never requirement_id values" in SOURCE_TRIAGE_SET_PROMPT
