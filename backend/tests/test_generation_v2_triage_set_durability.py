from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2 import source_triage_read_checkpointing as tracking
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_workspace_tool_handlers import (
    SourceTriageWorkspaceToolHandlers,
)
from app.generation_v2.source_triage_workspace_tool_specs import source_triage_set_tools
from app.generation_v2.source_triage_model_delivery import (
    finding_catalog_receipt_ref,
    question_workbench_receipt_ref,
    stage_finding_catalog_page,
    stage_question_workbench_page,
)
from app.generation_v2.source_triage_set_state_views import finding_catalog_page
from app.generation_v2.source_triage_set_state_views import question_workbench_page
from app.generation_v2.source_triage_set_state_views import question_workbench_signature
from app.generation_v2.pipeline.source_triage_set_flow import (
    _set_progress_state,
    _set_runtime_progress_state,
)
from app.models.generation_v2 import EvidenceStrength


class _EvidenceToolset:
    def __init__(
        self,
        evidence: dict[str, SimpleNamespace],
        refs: dict[str, str],
        *,
        exact_read_ids: set[str] | None = None,
    ) -> None:
        self.evidence = evidence
        self.refs = refs
        self.exact_read_ids = exact_read_ids

    async def _evidence_in_run(self, evidence_id: str):
        return self.evidence.get(evidence_id)

    async def _source_ref_for_id(self, source_id) -> str:
        return self.refs.get(str(source_id), "S??")

    async def _select_evidence_ids_for_write(self, _evidence_ids: list[str], **_kwargs):
        return []

    def _evidence_has_current_exact_read(self, evidence: SimpleNamespace) -> bool:
        return self.exact_read_ids is None or str(evidence.id) in self.exact_read_ids


def _evidence(evidence_id: str, source_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID(evidence_id),
        source_id=uuid.UUID(source_id),
        source_version_id=uuid.uuid4(),
        snippet_text="Exact grounded source text for the bounded finding.",
        locator_text="section 1",
        retrieval_channel="local_chunk_search",
        evidence_strength=EvidenceStrength.EXACT,
        metadata_={"usage_status": "observed"},
        purpose="source_read",
    )


def _workspace() -> dict:
    return {
        "schema_version": "source_triage_workspace.v3",
        "current_set": {"set_id": "set-1"},
        "source_order": ["S01", "S02"],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01", "S02"],
                "planned_question_ids": ["q1"],
                "question_ids": ["q1"],
                "cross_question_ids": [],
                "question_assignments": [
                    {"question_id": "q1", "requirement_ids": ["q1:r1"]}
                ],
                "observations": [],
                "written_actions": [],
                "evidence_ids": [],
            }
        ],
        "coverage_questions": [
            {
                "question_id": "q1",
                "question": "What happened?",
                "answer_requirements": [
                    {"requirement_id": "q1:r1", "description": "Establish the event."}
                ],
            },
            {
                "question_id": "q2",
                "question": "Why did it happen?",
                "answer_requirements": [
                    {"requirement_id": "q2:r1", "description": "Establish the cause."}
                ],
            },
        ],
        "source_assessments": [],
        "source_findings": [],
        "coverage_outcomes": [],
        "triage_semantic_revision": 0,
    }


def _card(source_id: str, title: str) -> SourceCard:
    return SourceCard(
        source_id=source_id,
        source_version_ids=[str(uuid.uuid4())],
        title=title,
        source_type="web",
        summary=f"{title} summary.",
    )


def _finding(
    *,
    finding_id: str,
    evidence_id: str,
    question_links: list[dict],
    source_relations: list[dict] | None = None,
) -> dict:
    return {
        "finding_id": finding_id,
        "statement": "The source establishes one bounded fact.",
        "evidence_ids": [evidence_id],
        "scope": {"actor": "Named actor", "period": "Named period", "geography": "", "qualifiers": []},
        "limitations": [],
        "question_links": question_links,
        "source_relations": source_relations or [],
    }


def _analysis_args(*, source_ref: str, findings: list[dict]) -> dict:
    return {
        "set_id": "set-1",
        "source_ref": source_ref,
        "processing_status": "resolved",
        "representation_status": "readable",
        "representation_reason": "The target text was read.",
        "relevance": "core",
        "relevance_reason": "It addresses a persisted question.",
        "authority": "official",
        "authority_reason": "It is an institutional publication.",
        "uniqueness": "unique",
        "uniqueness_reason": "It contributes an independently grounded fact.",
        "disposition": "anchor",
        "disposition_reason": "",
        "editorial_functions": [],
        "evidence_ids": [],
        "limitations": [],
        "confidence": 0.8,
        "findings": findings,
        "no_findings_reason": "",
    }


def _outcome_args(*, question_id: str, requirement_id: str, finding_id: str) -> dict:
    return {
        "set_id": "set-1",
        "question_id": question_id,
        "status": "covered",
        "finding_ids": [finding_id],
        "requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "covered",
                "finding_ids": [finding_id],
                "summary": "The answer part is established.",
                "missing_detail": "",
                "conflict_summary": "",
                "deferred_reason": "",
                "confidence": 0.8,
            }
        ],
        "orientation_summary": "The answer is established.",
        "missing_detail": "",
        "conflict_summary": "",
        "deferred_reason": "",
        "confidence": 0.8,
    }


def _patch_workspace(monkeypatch: pytest.MonkeyPatch, workspace: dict) -> dict:
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace
        return SimpleNamespace(payload=workspace)

    monkeypatch.setattr(workspace_api, "load_source_triage_workspace", load)
    monkeypatch.setattr(workspace_api, "save_source_triage_workspace", save)
    return state


def test_set_source_analysis_contract_defers_links_and_relations() -> None:
    async def handler(_args):
        return {"accepted": True}

    analysis_tool = next(
        tool
        for tool in source_triage_set_tools(handler, handler, handler)
        if tool.name == "save_source_analysis"
    )
    finding_properties = analysis_tool.parameters["properties"]["findings"]["items"][
        "properties"
    ]

    assert "passage_refs" in finding_properties
    assert "possible_question_ids" in finding_properties
    assert "question_links" not in finding_properties
    assert "source_relations" not in finding_properties


def test_set_runtime_progress_keeps_output_language_after_compaction() -> None:
    workspace = _workspace()
    workspace["intent"] = {"language": "ru"}

    progress = _set_runtime_progress_state(workspace, "set-1")

    assert progress["output_language"] == "ru"


@pytest.mark.asyncio
async def test_question_resolution_atomically_writes_links_and_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    workspace["source_findings"] = [
        {
            **_finding(
                finding_id="finding:S01:one",
                evidence_id=evidence_id,
                question_links=[],
            ),
            "set_id": "set-1",
            "source_ref": "S01",
            "source_id": source_id,
            "support_explanation": "The exact passage establishes the bounded event.",
        }
    ]
    workspace["source_assessments"] = [
        {
            "assessment_id": f"assessment:{source_id}",
            "set_id": "set-1",
            "source_ref": "S01",
            "source_id": source_id,
            "processing_status": "resolved",
            "representation_status": "readable",
            "disposition": "background",
            "finding_ids": ["finding:S01:one"],
            "no_findings_reason": "",
        }
    ]
    state = _patch_workspace(monkeypatch, workspace)
    catalog = finding_catalog_page(
        workspace,
        workspace["source_sets"][0],
        cursor=0,
        limit=100,
    )
    catalog_receipt_ref = finding_catalog_receipt_ref(
        set_id="set-1",
        snapshot_id=catalog["finding_catalog_snapshot_id"],
        cursor=0,
        returned_count=catalog["page"]["returned_count"],
    )
    stage_finding_catalog_page(
        workspace,
        source_set=workspace["source_sets"][0],
        payload=catalog,
        receipt_ref=catalog_receipt_ref,
    )
    workspace["source_triage_finding_catalog_deliveries"][0]["delivery_status"] = "delivered"
    page = question_workbench_page(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
        cursor=0,
        limit=100,
    )
    snapshot_id = page["workbench_snapshot_id"]
    receipt_ref = question_workbench_receipt_ref(
        set_id="set-1",
        question_id="q1",
        snapshot_id=snapshot_id,
        cursor=0,
        returned_count=page["page"]["returned_count"],
    )
    stage_question_workbench_page(
        workspace,
        source_set=workspace["source_sets"][0],
        question_id="q1",
        payload=page,
        receipt_ref=receipt_ref,
    )
    workspace["source_triage_question_workbench_deliveries"][0]["delivery_status"] = "delivered"
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set-1",
    )

    resolution_args = {
        "question_id": "q1",
        "workbench_snapshot_id": snapshot_id,
        "requirement_results": [
            {
                "requirement_id": "q1:r1",
                "status": "covered",
                "answer_points": [
                    {
                        "point": "Событие установлено.",
                        "finding_ids": ["finding:S01:one"],
                    }
                ],
                "criterion_match_reason": "Находка прямо устанавливает событие.",
                "unresolved_remainder": "",
                "conflict_summary": "",
                "deferred_reason": "",
            }
        ],
    }
    contradictory_args = {
        **resolution_args,
        "requirement_results": [
            {
                **resolution_args["requirement_results"][0],
                "unresolved_remainder": "Причина всё ещё не установлена.",
            }
        ],
    }
    rejected = await handler.upsert_coverage_outcome_tool(contradictory_args)

    assert rejected["error"] == "question_resolution_fields_contradict_status"
    assert state["workspace"]["source_findings"][0]["question_links"] == []
    assert state["workspace"]["coverage_outcomes"] == []

    unsupported_point_args = {
        **resolution_args,
        "requirement_results": [
            {
                **resolution_args["requirement_results"][0],
                "answer_points": [
                    {
                        "point": "Событие установлено, и добавлен новый факт без сохранённой находки.",
                        "finding_ids": ["finding:S01:not-saved"],
                    }
                ],
            }
        ],
    }
    unsupported = await handler.upsert_coverage_outcome_tool(unsupported_point_args)

    assert unsupported["error"] == "question_resolution_references_unknown_or_wrong_set_finding"
    assert unsupported["finding_id"] == "finding:S01:not-saved"
    assert state["workspace"]["source_findings"][0]["question_links"] == []
    assert state["workspace"]["coverage_outcomes"] == []

    deprecated_summary_args = {
        **resolution_args,
        "requirement_results": [
            {
                **resolution_args["requirement_results"][0],
                "established_answer": "Старый свободный итог больше не принимается.",
            }
        ],
    }
    deprecated = await handler.upsert_coverage_outcome_tool(deprecated_summary_args)

    assert deprecated["error"] == "question_resolution_established_answer_is_deprecated"
    assert "answer_points" in deprecated["next_action"]
    assert state["workspace"]["source_findings"][0]["question_links"] == []
    assert state["workspace"]["coverage_outcomes"] == []

    result = await handler.upsert_coverage_outcome_tool(resolution_args)

    assert result["accepted"] is True
    finding = state["workspace"]["source_findings"][0]
    assert finding["question_links"] == [
        {
            "question_id": "q1",
            "requirement_ids": ["q1:r1"],
            "rationale": "Находка прямо устанавливает событие.",
        }
    ]
    outcome = state["workspace"]["coverage_outcomes"][0]
    assert outcome["status"] == "covered"
    assert outcome["finding_ids"] == ["finding:S01:one"]
    assert outcome["requirement_results"][0]["answer_points"] == [
        {"point": "Событие установлено.", "finding_ids": ["finding:S01:one"]}
    ]
    assert outcome["requirement_results"][0]["summary"] == "Событие установлено."
    assert outcome["workbench_snapshot_id"] == snapshot_id


@pytest.mark.asyncio
async def test_stale_question_workbench_rejection_points_to_same_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    workspace["source_findings"] = [
        {
            **_finding(
                finding_id="finding:S01:one",
                evidence_id=evidence_id,
                question_links=[],
            ),
            "set_id": "set-1",
            "source_ref": "S01",
            "source_id": source_id,
            "support_explanation": "The exact passage establishes the bounded event.",
        }
    ]
    _patch_workspace(monkeypatch, workspace)
    page = question_workbench_page(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
        cursor=0,
        limit=100,
    )
    old_snapshot_id = page["workbench_snapshot_id"]
    workspace["source_findings"].append(
        {
            **_finding(
                finding_id="finding:S01:two",
                evidence_id=evidence_id,
                question_links=[],
            ),
            "set_id": "set-1",
            "source_ref": "S01",
            "source_id": source_id,
            "statement": "A later saved finding changes the current workbench snapshot.",
            "support_explanation": "It changes the model-visible finding catalog.",
        }
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set-1",
    )

    rejected = await handler.upsert_coverage_outcome_tool(
        {
            "set_id": "set-1",
            "question_id": "q1",
            "workbench_snapshot_id": old_snapshot_id,
            "requirement_results": [
                {
                    "requirement_id": "q1:r1",
                    "status": "covered",
                    "answer_points": [
                        {
                            "point": "Событие установлено.",
                            "finding_ids": ["finding:S01:one"],
                        }
                    ],
                    "criterion_match_reason": "Находка прямо устанавливает событие.",
                    "unresolved_remainder": "",
                    "conflict_summary": "",
                    "deferred_reason": "",
                }
            ],
        }
    )

    assert rejected["error"] == "question_workbench_snapshot_stale"
    assert rejected["required_read"] == {
        "section": "question_workbench",
        "set_id": "set-1",
        "question_id": "q1",
        "cursor": "0",
        "target_workbench_snapshot_id": rejected["current_workbench_snapshot_id"],
    }
    assert rejected["allowed_next_actions"][1]["arguments"]["question_id"] == "q1"
    assert "question_id='q1'" in rejected["next_action"]


@pytest.mark.asyncio
async def test_source_analysis_warns_that_read_workbenches_are_invalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set-1",
    )

    saved = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id="finding:S01:one",
                    evidence_id=evidence_id,
                    question_links=[],
                )
            ],
        )
    )

    assert saved["accepted"] is True
    assert saved["analysis_changed"] is True
    assert saved["invalidated_question_workbench_question_ids"] == ["q1"]
    assert "discard that old snapshot" in saved["next_action"]


@pytest.mark.asyncio
async def test_verified_duplicate_source_analysis_is_a_readable_source_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    workspace = _workspace()
    _patch_workspace(monkeypatch, workspace)

    async def exact_match(*_args, **_kwargs):
        return {"source_ref": "S01", "matched_representation_kinds": ["extracted_text"]}

    monkeypatch.setattr(
        "app.generation_v2.source_triage_passage_analysis.exact_duplicate_predecessor",
        exact_match,
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[_card(source_id_1, "Original"), _card(source_id_2, "Mirror")]
        ),
        evidence_toolset=None,
        current_set_id="set-1",
    )

    saved = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S02",
            "representation_status": "readable",
            "representation_reason": "",
            "source_summary": "Зеркальная копия уже разобранного источника.",
            "authority": "unknown",
            "authority_reason": "",
            "uniqueness": "duplicate",
            "uniqueness_reason": "Backend подтвердил идентичное представление с S01.",
            "duplicate_of_source_ref": "S01",
            "limitations": ["Самостоятельных новых находок нет."],
            "no_findings_reason": "Содержательно совпадает с S01, поэтому отдельные находки не дублируются.",
            "findings": [],
            "reading_basis": {
                "coverage_summary": "Содержимое закрыто через подтверждённый точный дубликат S01.",
                "unread_regions": "Отдельное повторное чтение S02 не требуется.",
                "unread_risk_assessment": "Идентичное представление не добавит нового содержания.",
                "representation_limits": "",
                "stopping_reason": "Точное совпадение представления подтверждено backend.",
            },
        }
    )

    assert saved["accepted"] is True
    stored = workspace["source_assessments"][0]
    assert stored["representation_status"] == "readable"
    assert stored["uniqueness"] == "duplicate"
    assert stored["duplicate_of_source_ref"] == "S01"
    assert stored["evidence_ids"] == []
    assert stored["reading_basis"]["verified_exact_duplicate_of_source_ref"] == "S01"


@pytest.mark.asyncio
async def test_verified_duplicate_rejects_unavailable_representation_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id_1 = str(uuid.uuid4())
    source_id_2 = str(uuid.uuid4())
    workspace = _workspace()
    _patch_workspace(monkeypatch, workspace)

    async def exact_match(*_args, **_kwargs):
        return {"source_ref": "S01", "matched_representation_kinds": ["extracted_text"]}

    monkeypatch.setattr(
        "app.generation_v2.source_triage_passage_analysis.exact_duplicate_predecessor",
        exact_match,
    )
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(
            source_cards=[_card(source_id_1, "Original"), _card(source_id_2, "Mirror")]
        ),
        evidence_toolset=None,
        current_set_id="set-1",
    )

    rejected = await handler.upsert_source_assessment_tool(
        {
            "source_ref": "S02",
            "representation_status": "unreadable",
            "representation_reason": "Модель ошибочно решила, что копию не надо читать.",
            "source_summary": "Зеркальная копия уже разобранного источника.",
            "authority": "unknown",
            "authority_reason": "",
            "uniqueness": "duplicate",
            "uniqueness_reason": "Backend подтвердил идентичное представление с S01.",
            "duplicate_of_source_ref": "S01",
            "limitations": [],
            "no_findings_reason": "Содержательно совпадает с S01.",
            "findings": [],
            "reading_basis": {
                "coverage_summary": "Содержимое закрыто через подтверждённый точный дубликат S01.",
                "unread_regions": "Отдельное повторное чтение S02 не требуется.",
                "unread_risk_assessment": "Идентичное представление не добавит нового содержания.",
                "representation_limits": "",
                "stopping_reason": "Точное совпадение представления подтверждено backend.",
            },
        }
    )

    assert rejected["accepted"] is False
    assert rejected["error"] == "verified_duplicate_must_be_saved_as_readable_target_content"
    assert rejected["corrected_fields"]["representation_status"] == "readable"
    assert rejected["corrected_fields"]["uniqueness"] == "duplicate"


def test_legacy_review_bookkeeping_does_not_count_as_semantic_progress() -> None:
    workspace = _workspace()
    before = _set_progress_state(workspace, "set-1")

    workspace["source_triage_set_semantic_reviews"] = {
        "set-1": {"signature": "snapshot", "read_part_indexes": [0], "complete": False}
    }
    after_review = _set_progress_state(workspace, "set-1")
    workspace["source_triage_set_semantic_self_checks"] = {
        "set-1": {"contract_version": "v2.semantic_self_check.2", "review_snapshot_id": "snapshot"}
    }
    after_self_check = _set_progress_state(workspace, "set-1")

    assert after_review == before
    assert after_self_check == after_review


@pytest.mark.asyncio
async def test_set_finishes_without_a_separate_semantic_review_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["intent"] = {"language": "ru"}
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    workspace["coverage_questions"][1]["parent_question_id"] = "q1"
    _patch_workspace(monkeypatch, workspace)
    evidence = _evidence(evidence_id, source_id)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset({evidence_id: evidence}, {source_id: "S01"}),
        current_set_id="set-1",
    )
    finding_id = "finding:S01:one"
    saved = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[
                        {
                            "question_id": "q1",
                            "requirement_ids": ["q1:r1"],
                            "rationale": "Direct support.",
                        }
                    ],
                )
            ],
        )
    )
    outcome = await handler.upsert_coverage_outcome_tool(
        _outcome_args(question_id="q1", requirement_id="q1:r1", finding_id=finding_id)
    )
    workspace["coverage_outcomes"][0]["workbench_snapshot_id"] = question_workbench_signature(
        workspace,
        workspace["source_sets"][0],
        question_id="q1",
    )
    finish_args = {
        "set_id": "set-1",
        "status": "mapped",
        "summary": "Источники разобраны, а результаты по вопросу сохранены.",
        "deferred_reason": "",
        "deferred_sources": [],
    }

    completed = await handler.finish_triage_source_set_tool(finish_args)

    assert saved["accepted"] is True
    assert outcome["accepted"] is True
    assert completed["accepted"] is True


@pytest.mark.asyncio
async def test_exact_read_requires_a_durable_checkpoint_before_source_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace()
    state = {"workspace": workspace}

    async def load(_db, _run_id):
        return state["workspace"]

    async def save(_db, *, workspace: dict, **_kwargs):
        state["workspace"] = workspace

    monkeypatch.setattr(tracking, "load_source_triage_workspace", load)
    monkeypatch.setattr(tracking, "save_source_triage_workspace", save)
    run_id = uuid.uuid4()

    advisory = await tracking.record_triage_exact_read(
        None,  # type: ignore[arg-type]
        generation_run_id=run_id,
        current_set_id="set-1",
        source_ref="S01",
        evidence_ids=["evidence-1"],
    )
    assert advisory is not None
    assert advisory["pending_source_refs"] == ["S01"]
    assert advisory["analysis_pending"] is True
    assert "before switching sources" in advisory["message"].casefold()

    tracking.clear_pending_exact_read(workspace["source_sets"][0], source_ref="S01")
    assert tracking.pending_exact_read_source_refs(
        workspace,
        source_set=workspace["source_sets"][0],
    ) == []


@pytest.mark.asyncio
async def test_set_relation_requires_evidence_from_both_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a, source_b = str(uuid.uuid4()), str(uuid.uuid4())
    evidence_a = str(uuid.uuid4())
    workspace = _workspace()
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_a, "A"), _card(source_b, "B")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_a: _evidence(evidence_a, source_a)},
            {source_a: "S01", source_b: "S02"},
        ),
        current_set_id="set-1",
    )

    result = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id="finding:S01:one",
                    evidence_id=evidence_a,
                    question_links=[
                        {
                            "question_id": "q1",
                            "requirement_ids": ["q1:r1"],
                            "rationale": "Direct support.",
                        }
                    ],
                    source_relations=[
                        {
                            "related_source_ref": "S02",
                            "relationship": "duplicate",
                            "rationale": "The texts are asserted to be the same.",
                            "evidence_ids": [evidence_a],
                        }
                    ],
                )
            ],
        )
    )

    assert result["accepted"] is False
    assert result["error"] == "A source relation requires grounded non-shell evidence from both sources"
    assert result["missing_evidence_source_refs"] == ["S02"]
    assert result["finding_index"] == 0


@pytest.mark.asyncio
async def test_search_preview_cannot_be_persisted_as_finding_before_exact_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_order"] = ["S01"]
    _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
            exact_read_ids=set(),
        ),
        current_set_id="set-1",
    )

    result = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id="finding:S01:one",
                    evidence_id=evidence_id,
                    question_links=[
                        {
                            "question_id": "q1",
                            "requirement_ids": ["q1:r1"],
                            "rationale": "Direct support.",
                        }
                    ],
                )
            ],
        )
    )

    assert result["accepted"] is False
    assert result["error"] == "Semantic triage evidence must have a current substantive exact read"
    assert result["not_exact_read_evidence_ids"] == [evidence_id]
    assert "read_evidence or read_source_window" in result["next_action"]


@pytest.mark.asyncio
async def test_discovered_question_attempt_requires_durable_link_or_explicit_reanalysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    evidence_id = str(uuid.uuid4())
    workspace = _workspace()
    workspace["source_sets"][0]["source_refs"] = ["S01"]
    workspace["source_sets"][0]["pending_exact_reads"] = [
        {"source_ref": "S01", "evidence_ids": [evidence_id]}
    ]
    workspace["source_order"] = ["S01"]
    state = _patch_workspace(monkeypatch, workspace)
    handler = SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=SourceMap(source_cards=[_card(source_id, "A")]),
        evidence_toolset=_EvidenceToolset(
            {evidence_id: _evidence(evidence_id, source_id)},
            {source_id: "S01"},
        ),
        current_set_id="set-1",
    )
    finding_id = "finding:S01:one"
    q1_link = {
        "question_id": "q1",
        "requirement_ids": ["q1:r1"],
        "rationale": "Direct support.",
    }
    saved = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[q1_link],
                )
            ],
        )
    )
    rejected = await handler.upsert_coverage_outcome_tool(
        _outcome_args(question_id="q2", requirement_id="q2:r1", finding_id=finding_id)
    )

    assert saved["accepted"] is True
    assert state["workspace"]["source_sets"][0]["pending_exact_reads"] == []
    assert rejected["accepted"] is False
    assert rejected["lineage_errors"] == [
        {
            "question_id": "q2",
            "requirement_id": "q2:r1",
            "finding_id": finding_id,
            "source_ref": "S01",
            "reason": "finding_not_linked_to_requirement",
            "corrective_action": (
                "Resave this source's complete analysis with the finding linked to the exact question and "
                "requirement; otherwise remove this finding from the outcome."
            ),
        }
    ]
    from app.generation_v2.runtime_tool_output import model_facing_tool_output

    assert model_facing_tool_output("upsert_coverage_outcome", rejected) == rejected
    pending = state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"]
    assert pending[0]["question_id"] == "q2"
    assert pending[0]["source_refs"] == ["S01"]

    blocked_finish = await handler.finish_triage_source_set_tool(
        {
            "set_id": "set-1",
            "status": "mapped",
            "summary": "The set is mapped.",
            "deferred_reason": "",
            "deferred_sources": [],
        }
    )
    assert blocked_finish["accepted"] is False
    assert blocked_finish["pending_question_attempts"][0]["question_id"] == "q2"
    assert blocked_finish["pending_question_attempts"][0]["unreviewed_source_refs"] == ["S01"]

    confirmed_absent = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[q1_link],
                )
            ],
        )
    )
    assert confirmed_absent["accepted"] is True
    assert confirmed_absent["analysis_changed"] is False
    assert state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"] == []
    assert "q2" not in state["workspace"]["source_sets"][0]["question_ids"]

    corrected = await handler.upsert_source_assessment_tool(
        _analysis_args(
            source_ref="S01",
            findings=[
                _finding(
                    finding_id=finding_id,
                    evidence_id=evidence_id,
                    question_links=[
                        q1_link,
                        {
                            "question_id": "q2",
                            "requirement_ids": ["q2:r1"],
                            "rationale": "The same passage also establishes the causal answer part.",
                        },
                    ],
                )
            ],
        )
    )
    accepted = await handler.upsert_coverage_outcome_tool(
        _outcome_args(question_id="q2", requirement_id="q2:r1", finding_id=finding_id)
    )

    assert corrected["accepted"] is True
    assert "q2" in state["workspace"]["source_sets"][0]["question_ids"]
    assert state["workspace"]["source_sets"][0]["pending_discovered_question_attempts"] == []
    assert accepted["accepted"] is True
