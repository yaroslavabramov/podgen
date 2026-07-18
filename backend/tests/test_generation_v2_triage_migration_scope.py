from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_module
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_corpus_run_view import current_attempt_has_triage_history
from app.generation_v2.source_triage_workspace import ensure_source_triage_workspace
from app.generation_v2.source_triage_workspace_state import WORKSPACE_SCHEMA_VERSION
from app.generation_v2.tools.evidence_source_map_actions import (
    build_source_map_from_write_actions,
)
from app.models.generation_v2 import V2ToolTrace


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self):
        return self.values

    def scalar_one_or_none(self):
        if not self.values:
            return None
        if len(self.values) != 1:
            raise AssertionError("Expected at most one result")
        return self.values[0]


@pytest.mark.asyncio
async def test_legacy_triage_workspace_cannot_migrate_inside_the_same_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "schema_version": "source_triage_workspace.v2",
        "manifest_hash": "same-manifest",
        "corpus_revision": 1,
        "attempt": 1,
        "status": "in_progress",
        "source_order": ["S01"],
    }
    saved: list[dict] = []

    async def load_workspace(*_args, **_kwargs):
        return existing

    async def save_workspace(*_args, **kwargs):
        saved.append(kwargs["workspace"])
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(workspace_module, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(workspace_module, "_source_order", lambda _inventory: ["S01"])

    with pytest.raises(RuntimeError, match="requires a fresh corpus attempt"):
        await ensure_source_triage_workspace(
            None,  # type: ignore[arg-type]
            run=SimpleNamespace(id=uuid.uuid4(), config={}),  # type: ignore[arg-type]
            run_view=SimpleNamespace(
                manifest_hash="same-manifest",
                revision=1,
                attempt=1,
            ),
            inventory=SourceMap(),
        )

    assert saved == []


@pytest.mark.asyncio
async def test_legacy_triage_workspace_starts_clean_in_a_fresh_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "schema_version": "source_triage_workspace.v2",
        "manifest_hash": "same-manifest",
        "corpus_revision": 1,
        "attempt": 1,
        "status": "in_progress",
        "source_order": ["S01"],
        "source_sets": [{"set_id": "legacy", "source_refs": ["S01"]}],
        "source_assessments": [{"source_ref": "S01", "evidence_ids": [str(uuid.uuid4())]}],
        "coverage_outcomes": [{"question_id": "legacy-question", "status": "covered"}],
        "repair_operation": {"status": "pending", "kind": "source_triage"},
    }
    saved: list[dict] = []

    async def load_workspace(*_args, **_kwargs):
        return existing

    async def save_workspace(*_args, **kwargs):
        saved.append(kwargs["workspace"])
        return SimpleNamespace(id=uuid.uuid4())

    def forbidden_repair_carry(*_args, **_kwargs):
        raise AssertionError("Legacy V2 repair state must not be carried into V3")

    async def forbidden_lineage_carry(*_args, **_kwargs):
        raise AssertionError("Legacy evidence/actions must not be carried into V3")

    monkeypatch.setattr(workspace_module, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(workspace_module, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(workspace_module, "_source_order", lambda _inventory: ["S01"])
    monkeypatch.setattr(workspace_module, "carry_pending_triage_repair", forbidden_repair_carry)
    monkeypatch.setattr(
        workspace_module,
        "_carry_evidence_to_current_attempt",
        forbidden_lineage_carry,
    )
    monkeypatch.setattr(
        workspace_module,
        "_carry_source_map_write_actions_to_current_attempt",
        forbidden_lineage_carry,
    )

    workspace = await ensure_source_triage_workspace(
        None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), config={}),  # type: ignore[arg-type]
        run_view=SimpleNamespace(
            manifest_hash="same-manifest",
            revision=1,
            attempt=2,
        ),
        inventory=SourceMap(),
    )

    assert workspace["schema_version"] == WORKSPACE_SCHEMA_VERSION
    assert workspace["attempt"] == 2
    assert workspace["source_sets"] == []
    assert workspace["source_assessments"] == []
    assert workspace["source_findings"] == []
    assert workspace["coverage_outcomes"] == []
    assert workspace.get("repair_operation") is None
    assert saved == [workspace]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "expected_query_count", "expected"),
    [
        ([[], [SimpleNamespace(id=uuid.uuid4())]], 2, True),
        ([[], [], [SimpleNamespace(id=uuid.uuid4())]], 3, True),
        ([[], [], []], 3, False),
    ],
)
async def test_triage_history_detection_includes_legacy_traces_and_evidence(
    responses: list[list[object]],
    expected_query_count: int,
    expected: bool,
) -> None:
    class Db:
        def __init__(self) -> None:
            self.responses = [_ScalarResult(items) for items in responses]
            self.queries: list[object] = []

        async def execute(self, statement):
            self.queries.append(statement)
            return self.responses.pop(0)

    db = Db()
    history_exists = await current_attempt_has_triage_history(
        db,  # type: ignore[arg-type]
        SimpleNamespace(
            generation_run_id=uuid.uuid4(),
            revision=4,
            attempt=3,
        ),
    )

    assert history_exists is expected
    assert len(db.queries) == expected_query_count
    for query in db.queries:
        params = query.compile().params
        assert _query_param(params, "corpus_revision") == 4
        assert _query_param(params, "attempt") == 3


class _ScopeFilteringDb:
    def __init__(self, traces: list[object], evidence: list[object]) -> None:
        self.traces = traces
        self.evidence = evidence
        self.query_scopes: list[tuple[int | None, int | None]] = []

    async def execute(self, statement):
        params = statement.compile().params
        revision = _query_param(params, "corpus_revision")
        attempt = _query_param(params, "attempt")
        self.query_scopes.append((revision, attempt))
        entity = statement.column_descriptions[0].get("entity")
        values = self.traces if entity is V2ToolTrace else self.evidence
        return _ScalarResult(
            [
                item
                for item in values
                if (revision is None or item.corpus_revision == revision)
                and (attempt is None or item.attempt == attempt)
            ]
        )


def _query_param(params: dict, column: str) -> int | None:
    return next(
        (
            int(value)
            for key, value in params.items()
            if key == column or key.startswith(f"{column}_")
        ),
        None,
    )


@pytest.mark.asyncio
async def test_source_map_materialization_excludes_prior_attempt_actions_and_evidence() -> None:
    run_id = uuid.uuid4()
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    old_evidence_id = uuid.uuid4()
    current_evidence_id = uuid.uuid4()

    def trace(*, attempt: int, summary: str, evidence_id: uuid.UUID) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            generation_run_id=run_id,
            corpus_revision=1,
            attempt=attempt,
            phase="v2_source_triage",
            tool_name="upsert_source_card",
            input_json={
                "source_ref": "S01",
                "summary": summary,
                "useful_for": ["context"],
                "evidence_ids": [str(evidence_id)],
                "gaps_or_cautions": [],
            },
            output_json={"accepted": True},
            created_at=None,
        )

    def evidence(*, attempt: int, evidence_id: uuid.UUID, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=evidence_id,
            generation_run_id=run_id,
            corpus_revision=1,
            attempt=attempt,
            source_id=source_id,
            source_version_id=version_id,
            metadata_={"usage_status": "selected", "selected_role": "context"},
            snippet_text=text,
            locator_text="section 1",
            retrieval_channel="local_chunk_search",
            purpose="source_read",
        )

    old_trace = trace(
        attempt=1,
        summary="OLD ATTEMPT MUST NOT LEAK",
        evidence_id=old_evidence_id,
    )
    current_trace = trace(
        attempt=2,
        summary="Current attempt summary",
        evidence_id=current_evidence_id,
    )
    finalize = SimpleNamespace(
        id=uuid.uuid4(),
        generation_run_id=run_id,
        corpus_revision=1,
        attempt=2,
        phase="v2_source_triage",
        tool_name="finalize_source_map",
        input_json={"ready": True, "summary": "Current finalization"},
        output_json={"accepted": True},
        created_at=None,
    )
    db = _ScopeFilteringDb(
        [old_trace, current_trace, finalize],
        [
            evidence(
                attempt=1,
                evidence_id=old_evidence_id,
                text="Old attempt evidence.",
            ),
            evidence(
                attempt=2,
                evidence_id=current_evidence_id,
                text="Current attempt evidence.",
            ),
        ],
    )
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=str(source_id),
                source_version_ids=[str(version_id)],
                title="Source",
                source_type="web",
                summary="Inventory summary",
            )
        ]
    )

    source_map = await build_source_map_from_write_actions(
        db,  # type: ignore[arg-type]
        generation_run_id=run_id,
        inventory=inventory,
        run_view=SimpleNamespace(revision=1, attempt=2),
    )

    assert db.query_scopes == [(1, 2), (1, 2)]
    assert source_map.source_cards[0].summary == "Current attempt summary"
    assert source_map.evidence_ids == [str(current_evidence_id)]
    assert source_map.retrieval_notes == ["Current finalization"]
