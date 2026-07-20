from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.schemas import SourceCard, SourceMap
from app.generation_v2.source_triage_workspace_prompting import source_set_prompt_payload
from app.generation_v2.source_triage_workspace_state import normalize_workspace
from app.generation_v2.source_triage_workspace_tool_handlers import (
    SourceTriageWorkspaceToolHandlers,
)
from app.generation_v2.source_triage_workspace_tool_specs import source_triage_set_tools
from app.generation_v2.source_triage_workspace_tools import _current_set_state_page


_PINNED_TEXT = "R" * 20_000


def _workspace(*, source_version_id: str) -> dict:
    return {
        "schema_version": "source_triage_workspace.v3",
        "current_set": {"set_id": "set-1"},
        "source_order": ["S01", "S02"],
        "source_sets": [
            {
                "set_id": "set-1",
                "status": "in_progress",
                "source_refs": ["S01"],
                "planned_question_ids": ["q1"],
                "question_ids": ["q1"],
                "cross_question_ids": [],
                "question_assignments": [],
                "evidence_ids": [],
            }
        ],
        "coverage_questions": [{"question_id": "q1", "question": "What happened?"}],
        "source_read_receipts": [
            {
                "receipt_ref": "receipt:one",
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": source_version_id,
                "content_sha256": hashlib.sha256(_PINNED_TEXT.encode("utf-8")).hexdigest(),
                "start_offset": 0,
                "end_offset": 8_000,
                "total_chars": 20_000,
                "read_kind": "sequential",
                "next_cursor": "receipt:one",
                "eof": False,
                "created_at": "2026-07-18T00:00:00",
            }
        ],
        "reading_notes": [],
        "source_assessments": [],
        "source_findings": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
    }


def _inventory(*, source_id: str, source_version_id: str) -> SourceMap:
    return SourceMap(
        source_cards=[
            SourceCard(
                source_id=source_id,
                source_version_ids=[source_version_id],
                title="Source one",
                source_type="web",
                summary="A readable source.",
            )
        ]
    )


def _note_args(**overrides) -> dict:
    payload = {
        "note_id": "",
        "source_ref": "S01",
        "receipt_refs": ["receipt:one"],
        "what_was_read": "The opening and mechanism sections.",
        "what_was_established": "The named actor used the described mechanism.",
        "boundaries": "The statement applies only to the documented period.",
        "unread_regions": "",
        "why_unread_is_safe": "",
        "question_links": [{"question_id": "q1", "connection": "Directly describes the event."}],
        "doubts": ["The later outcome is not yet established."],
        "next_checks": ["Read the remaining section."],
    }
    payload.update(overrides)
    return payload


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


def _handler(*, workspace: dict, source_id: str, source_version_id: str):
    receipt = next(
        (
            item
            for item in workspace.get("source_read_receipts") or []
            if str(item.get("source_version_id") or "") == source_version_id
        ),
        {},
    )
    version = SimpleNamespace(
        id=uuid.UUID(source_version_id),
        content_hash=str(receipt.get("content_sha256") or ""),
        receipt_text=_PINNED_TEXT,
    )

    class _EvidenceToolset:
        async def _resolve_source_version(self, **_kwargs):
            return version

    return SourceTriageWorkspaceToolHandlers(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        workspace=workspace,
        inventory=_inventory(source_id=source_id, source_version_id=source_version_id),
        evidence_toolset=_EvidenceToolset(),
        current_set_id="set-1",
    )


@pytest.fixture(autouse=True)
def _stub_pinned_source_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2 import source_triage_workspace_reading_handlers

    monkeypatch.setattr(
        source_triage_workspace_reading_handlers,
        "source_version_text",
        lambda version: str(getattr(version, "receipt_text", "")),
    )


def test_reading_note_contract_has_full_untruncated_semantic_fields() -> None:
    async def handler(_args):
        return {"accepted": True}

    tool = next(
        item
        for item in source_triage_set_tools(
            handler,
            handler,
            handler,
            save_reading_note_handler=handler,
        )
        if item.name == "save_reading_note"
    )
    properties = tool.parameters["properties"]

    for field in (
        "what_was_read",
        "what_was_established",
        "boundaries",
        "unread_regions",
        "why_unread_is_safe",
    ):
        assert "maxLength" not in properties[field]
    assert "maxItems" not in properties["doubts"]
    assert "maxItems" not in properties["next_checks"]


def test_receipt_only_state_resumes_after_failure_before_note_save() -> None:
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)

    prompt_payload = source_set_prompt_payload(workspace, "set-1")
    state_page = _current_set_state_page(
        workspace,
        workspace["source_sets"][0],
        cursor=0,
        limit=25,
    )

    assert prompt_payload["set"]["execution_mode"] == "resume"
    assert prompt_payload["saved_state_previews"]["latest_source_read_receipts"] == [
        {
            "receipt_ref": "receipt:one",
            "source_ref": "S01",
            "start_offset": 0,
            "end_offset": 8_000,
            "total_chars": 20_000,
            "read_kind": "sequential",
            "delivery_status": "delivered",
            "next_cursor": "receipt:one",
            "eof": False,
        }
    ]
    assert state_page["state"]["source_read_receipt_count"] == 1
    assert state_page["state"]["saved_reading_note_count"] == 0
    assert "receipt:one" in json.dumps(state_page)


def test_resume_state_exposes_last_receipt_for_every_source() -> None:
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    workspace["source_sets"][0]["source_refs"] = ["S01", "S02"]
    first = workspace["source_read_receipts"][0]
    workspace["source_read_receipts"] = [
        {**first, "receipt_ref": "receipt:s01-old", "end_offset": 4_000},
        {
            **first,
            "receipt_ref": "receipt:s02-last",
            "source_ref": "S02",
            "next_cursor": "",
            "eof": True,
        },
        {
            **first,
            "receipt_ref": "receipt:s01-last",
            "end_offset": 8_000,
            "next_cursor": "receipt:s01-last",
        },
    ]

    prompt_payload = source_set_prompt_payload(workspace, "set-1")
    state_page = _current_set_state_page(
        workspace,
        workspace["source_sets"][0],
        cursor=0,
        limit=25,
    )

    latest = prompt_payload["saved_state_previews"]["latest_source_read_receipts"]
    assert [item["receipt_ref"] for item in latest] == ["receipt:s01-last", "receipt:s02-last"]
    assert latest[0]["next_cursor"] == "receipt:s01-last"
    assert latest[0]["eof"] is False
    assert latest[1]["next_cursor"] == ""
    assert latest[1]["eof"] is True
    assert [
        item["receipt_ref"] for item in state_page["state"]["latest_source_read_receipts"]
    ] == ["receipt:s01-last", "receipt:s02-last"]


@pytest.mark.asyncio
async def test_reading_note_persists_full_semantics_and_returns_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    state = _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )
    long_read = "R" * 5_000
    long_established = "E" * 5_000

    saved = await handler.save_reading_note_tool(
        _note_args(
            what_was_read=long_read,
            what_was_established=long_established,
            unread_regions="Appendices A-C.",
            why_unread_is_safe="They contain only the referenced raw tables.",
        )
    )

    assert saved["accepted"] is True
    assert saved["saved_state"]["reading_note_count"] == 1
    note = state["workspace"]["reading_notes"][0]
    assert note["what_was_read"] == long_read
    assert note["what_was_established"] == long_established
    assert note["unread_regions"] == "Appendices A-C."
    assert note["why_unread_is_safe"] == "They contain only the referenced raw tables."

    prompt_payload = source_set_prompt_payload(state["workspace"], "set-1")
    assert prompt_payload["set"]["execution_mode"] == "resume"
    assert prompt_payload["saved_state_previews"]["reading_note_preview_count"] == 1
    assert prompt_payload["saved_state_previews"]["source_read_receipt_count"] == 1

    state_pages = []
    cursor = 0
    while True:
        state_page = _current_set_state_page(
            state["workspace"],
            state["workspace"]["source_sets"][0],
            cursor=cursor,
            limit=25,
        )
        state_pages.append(state_page)
        next_cursor = str(state_page["page"]["next_cursor"] or "")
        if not next_cursor:
            break
        cursor = int(next_cursor)
    encoded_state = json.dumps(state_pages, ensure_ascii=False)
    assert all(page["accepted"] is True for page in state_pages)
    assert state_pages[0]["state"]["source_read_receipt_count"] == 1
    assert state_pages[0]["state"]["saved_reading_note_count"] == 1
    assert "receipt:one" in encoded_state
    assert long_read in encoded_state
    assert long_established in encoded_state


@pytest.mark.asyncio
async def test_reading_note_rejects_unissued_or_cross_source_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    workspace["source_read_receipts"].append(
        {
            **workspace["source_read_receipts"][0],
            "receipt_ref": "receipt:other-source",
            "source_ref": "S02",
        }
    )
    _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    unissued = await handler.save_reading_note_tool(
        _note_args(receipt_refs=["receipt:not-issued"])
    )
    wrong_source = await handler.save_reading_note_tool(
        _note_args(receipt_refs=["receipt:other-source"])
    )

    assert unissued["accepted"] is False
    assert unissued["unknown_receipt_refs"] == ["receipt:not-issued"]
    assert wrong_source["accepted"] is False
    assert wrong_source["invalid_receipt_refs"] == ["receipt:other-source"]


@pytest.mark.asyncio
async def test_reading_note_rejects_pending_text_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    workspace["source_read_receipts"][0]["delivery_status"] = "pending"
    _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    result = await handler.save_reading_note_tool(_note_args())

    assert result["accepted"] is False
    assert result["invalid_receipt_refs"] == ["receipt:one"]


@pytest.mark.asyncio
async def test_reading_note_rejects_receipt_that_no_longer_matches_pinned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    workspace["source_read_receipts"][0]["content_sha256"] = "0" * 64
    state = _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    result = await handler.save_reading_note_tool(_note_args())

    assert result["accepted"] is False
    assert result["invalid_receipt_refs"] == ["receipt:one"]
    assert state["workspace"]["reading_notes"] == []


@pytest.mark.asyncio
async def test_oversized_note_field_is_rejected_never_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    state = _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    result = await handler.save_reading_note_tool(
        _note_args(what_was_established="E" * 20_000)
    )

    assert result["accepted"] is False
    assert result["error"] == "reading_note_field_exceeds_model_read_contract"
    assert result["field"] == "what_was_established"
    assert state["workspace"]["reading_notes"] == []


@pytest.mark.asyncio
async def test_native_file_receipt_is_valid_note_provenance_but_has_no_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = str(uuid.uuid4())
    source_version_id = str(uuid.uuid4())
    workspace = _workspace(source_version_id=source_version_id)
    workspace["source_read_receipts"] = [
        {
            "receipt_ref": "receipt:native",
            "set_id": "set-1",
            "source_ref": "S01",
            "source_version_id": source_version_id,
            "content_sha256": "b" * 64,
            "start_offset": 0,
            "end_offset": 0,
            "total_chars": 0,
            "read_kind": "native_file_attachment",
            "delivery_status": "pending",
            "created_at": "2026-07-18T00:00:00",
        }
    ]
    state = _patch_workspace(monkeypatch, workspace)
    handler = _handler(
        workspace=workspace,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    pending = await handler.save_reading_note_tool(
        _note_args(
            receipt_refs=["receipt:native"],
            what_was_read="The original file was delivered to the agent.",
            unread_regions="Completeness was not established by the attachment receipt.",
            why_unread_is_safe="",
        )
    )

    assert pending["accepted"] is False
    assert pending["invalid_receipt_refs"] == ["receipt:native"]

    workspace["source_read_receipts"][0]["delivery_status"] = "delivered"
    saved = await handler.save_reading_note_tool(
        _note_args(
            receipt_refs=["receipt:native"],
            what_was_read="The original file was delivered to the agent.",
            unread_regions="Completeness was not established by the attachment receipt.",
            why_unread_is_safe="",
        )
    )

    assert saved["accepted"] is True
    assert state["workspace"]["reading_notes"][0]["receipt_refs"] == ["receipt:native"]


def test_workspace_normalization_initializes_reading_memory() -> None:
    normalized = normalize_workspace({"schema_version": "source_triage_workspace.v3"})

    assert normalized["source_read_receipts"] == []
    assert normalized["reading_notes"] == []
