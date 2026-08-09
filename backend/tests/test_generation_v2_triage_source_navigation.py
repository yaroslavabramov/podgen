from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.generation_v2.tools.evidence_local_source import EvidenceLocalSourceMixin
from app.generation_v2.tools.evidence_local_windows import (
    read_source_text_page,
    read_source_window,
    search_source_text_ranges,
)
from app.generation_v2.tools.evidence_model_window_refs import resolve_model_window_ref
from app.generation_v2.tools.evidence_source_tools import EvidenceSourceToolsMixin
from app.generation_v2.tools.evidence_tool_source_read_specs import evidence_source_read_tools
from app.generation_v2.tools.evidence_window_refs import resolve_window_ref
from app.models.generation_v2 import SourceRepresentationKind, SourceVersion, V2RecordStatus


class _Db:
    async def get(self, _model: Any, _item_id: Any) -> None:
        return None


def _passage_text(payload: dict[str, Any]) -> str:
    return "".join(str(item.get("text") or "") for item in payload.get("passages") or [])


def test_source_passages_are_lossless_bounded_and_stable() -> None:
    from app.generation_v2.tools.evidence_source_passages import (
        SOURCE_PASSAGE_MAX_CHARS,
        resolve_source_passage_ref,
        source_passages_for_range,
    )

    version_id = uuid.uuid4()
    text = "\n\n".join(f"Section {index}. " + ("content " * 220) for index in range(8))
    first = source_passages_for_range(
        source_version_id=version_id,
        text=text,
        start_offset=0,
        end_offset=len(text),
        allow_timestamps=False,
    )
    repeated = source_passages_for_range(
        source_version_id=version_id,
        text=text,
        start_offset=0,
        end_offset=len(text),
        allow_timestamps=False,
    )

    assert first == repeated
    assert "".join(item["text"] for item in first) == text
    assert all(len(item["text"]) <= SOURCE_PASSAGE_MAX_CHARS for item in first)
    for item in first:
        resolved = resolve_source_passage_ref(
            item["passage_ref"],
            source_version_id=version_id,
            text=text,
        )
        assert text[resolved["start_offset"] : resolved["end_offset"]] == item["text"]


def _version() -> SourceVersion:
    return SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=SourceRepresentationKind.EXTRACTED_TEXT,
        storage_path="source.txt",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        metadata_={},
    )


class _NavigationOwner(EvidenceSourceToolsMixin, EvidenceLocalSourceMixin):
    def __init__(self, version: SourceVersion) -> None:
        self.version = version
        self.db = _Db()
        self.run = SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id)
        self.triage_current_set_id = "set-1"

    async def _triage_read_guard_error(self, **_kwargs: Any) -> None:
        return None

    async def _invalid_source_or_version_ref_error(self, **_kwargs: Any) -> None:
        return None

    async def _resolve_source_version(self, **_kwargs: Any) -> SourceVersion:
        return self.version

    async def _source_ref_for_id(self, _source_id: Any) -> str:
        return "S01"

    async def _source_id_from_ref_or_id(self, _source_ref: str) -> str:
        return str(self.version.source_id)

    async def _mark_evidence_exact_read(self, _evidence: Any, **_kwargs: Any) -> bool:
        return True

    async def _evidence_detail_payload(self, evidence: Any, *, max_snippet_chars: int) -> dict[str, Any]:
        return {
            "evidence_id": str(evidence.id),
            "snippet_text": str(evidence.snippet_text)[:max_snippet_chars],
            "locator_text": str(evidence.locator_text),
        }

    async def _record_triage_exact_read(self, **_kwargs: Any) -> None:
        return None


def test_read_source_tool_declares_checkpoint_before_source_switch() -> None:
    tool = next(
        item
        for item in evidence_source_read_tools(_NavigationOwner(_version()))
        if item.name == "read_source"
    )

    assert "before_opening_different_source.required" in tool.description
    assert "before calling any read tool for a different source" in tool.description
    assert "max_chars" not in tool.parameters["required"]


@pytest.mark.asyncio
async def test_read_source_omitted_max_chars_uses_largest_stable_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_source_tools

    text = "A" * 30_000
    version = _version()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)

    result = await _NavigationOwner(version).read_source_tool({"source_ref": "S01", "cursor": ""})

    assert result["found"] is True
    assert result["range"]["end_offset"] == 20_000
    assert result["returned_chars"] == 20_000


@pytest.mark.asyncio
async def test_read_source_null_max_chars_is_treated_as_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_source_tools

    text = "A" * 30_000
    version = _version()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)

    result = await _NavigationOwner(version).read_source_tool(
        {"source_ref": "S01", "cursor": "", "max_chars": None}
    )

    assert result["found"] is True
    assert result["range"]["end_offset"] == 20_000
    assert result["returned_chars"] == 20_000


def test_source_text_is_extracted_once_per_immutable_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source

    calls = 0

    def load(_version: SourceVersion) -> str:
        nonlocal calls
        calls += 1
        return "full source text"

    monkeypatch.setattr(evidence_local_source, "_source_version_text", load)
    owner = EvidenceLocalSourceMixin()
    version = _version()

    assert owner._cached_source_version_text(version) == "full source text"
    assert owner._cached_source_version_text(version) == "full source text"
    assert calls == 1


def test_transient_source_text_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source
    from app.generation_v2.tools.evidence_source_text import SourceTextLoadResult

    loads = [
        SourceTextLoadResult(text="", status="storage_unavailable", error_detail="TimeoutError"),
        SourceTextLoadResult(text="full source text", status="ready"),
    ]
    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: loads.pop(0))
    owner = EvidenceLocalSourceMixin()
    version = _version()

    assert owner._cached_source_version_text(version) == ""
    assert owner._source_version_text_error_payload(version) == {
        "error": "source_text_storage_temporarily_unavailable",
        "retryable": True,
    }
    assert owner._cached_source_version_text(version) == "full source text"
    assert owner._source_version_text_error_payload(version) is None


def test_fresh_activation_replays_delivered_reads_until_meaning_is_durable() -> None:
    from app.generation_v2.source_triage_workspace_prompting import source_set_prompt_payload

    first_ref = "receipt:first"
    second_ref = "receipt:second"
    native_ref = "receipt:native"
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": [],
                "status": "in_progress",
            }
        ],
        "coverage_questions": [],
        "source_assessments": [],
        "reading_notes": [],
        "source_read_receipts": [
            {
                "receipt_ref": first_ref,
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "sequential",
                "start_offset": 0,
                "end_offset": 1_000,
                "request": {"cursor": "", "max_chars": 1_000},
                "delivery_status": "delivered",
                "delivered_at": "2026-01-01T10:00:00",
            },
            {
                "receipt_ref": second_ref,
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-1",
                "read_kind": "sequential",
                "start_offset": 1_000,
                "end_offset": 2_000,
                "request": {"cursor": first_ref, "max_chars": 1_000},
                "delivery_status": "delivered",
                "delivered_at": "2026-01-01T10:01:00",
            },
            {
                "receipt_ref": native_ref,
                "set_id": "set-1",
                "source_ref": "S01",
                "source_version_id": "version-native",
                "read_kind": "native_file_attachment",
                "start_offset": 0,
                "end_offset": 0,
                "request": {"reading_goal": "Inspect the table and its footnote."},
                "delivery_status": "delivered",
                "delivered_at": "2026-01-01T10:00:30",
            },
        ],
    }

    replay = source_set_prompt_payload(workspace, "set-1")["saved_state_previews"][
        "replay_required_after_activation"
    ]

    assert [item["receipt_ref"] for item in replay] == [first_ref, second_ref, native_ref]
    assert replay[0]["retry"] == {
        "tool": "read_source",
        "source_ref": "S01",
        "cursor": "",
        "max_chars": 1_000,
    }
    assert replay[1]["retry"]["cursor"] == first_ref
    assert replay[2]["retry"] == {
        "tool": "open_source_file",
        "source_ref": "S01",
        "reading_goal": "Inspect the table and its footnote.",
    }

    workspace["reading_notes"] = [
        {
            "set_id": "set-1",
            "source_ref": "S01",
            "receipt_refs": [first_ref, native_ref],
            "what_was_read": "The first range and the native table were read.",
        }
    ]
    replay = source_set_prompt_payload(workspace, "set-1")["saved_state_previews"][
        "replay_required_after_activation"
    ]
    assert [item["receipt_ref"] for item in replay] == [second_ref]

    workspace["source_assessments"] = [
        {
            "set_id": "set-1",
            "source_ref": "S01",
            "processing_status": "resolved",
            "finding_ids": [],
            "no_findings_reason": "The completed analysis found no relevant material.",
            "updated_at": "2026-01-01T10:01:30",
        }
    ]
    assert (
        source_set_prompt_payload(workspace, "set-1")["saved_state_previews"][
            "replay_required_after_activation"
        ]
        == []
    )

    third_ref = "receipt:third"
    workspace["source_read_receipts"].append(
        {
            "receipt_ref": third_ref,
            "set_id": "set-1",
            "source_ref": "S01",
            "source_version_id": "version-1",
            "read_kind": "sequential",
            "start_offset": 2_000,
            "end_offset": 3_000,
            "request": {"cursor": second_ref, "max_chars": 1_000},
            "delivery_status": "delivered",
            "delivered_at": "2026-01-01T10:02:00",
        }
    )
    replay = source_set_prompt_payload(workspace, "set-1")["saved_state_previews"][
        "replay_required_after_activation"
    ]
    assert [item["receipt_ref"] for item in replay] == [third_ref]

    workspace["source_assessments"][0]["updated_at"] = "2026-01-01T10:03:00"
    assert (
        source_set_prompt_payload(workspace, "set-1")["saved_state_previews"][
            "replay_required_after_activation"
        ]
        == []
    )


@pytest.mark.asyncio
async def test_read_source_reports_transient_storage_failure_and_succeeds_on_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_source_tools
    from app.generation_v2.tools.evidence_source_text import SourceTextLoadResult

    text = "A complete source passage with enough content for a real read. " * 30
    loads = [
        SourceTextLoadResult(text="", status="storage_unavailable", error_detail="TimeoutError"),
        SourceTextLoadResult(text=text, status="ready"),
    ]
    version = _version()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: loads.pop(0))
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)

    owner = _NavigationOwner(version)
    first = await owner.read_source_tool(
        {"source_ref": "S01", "cursor": "", "max_chars": 1_000}
    )

    assert first == {
        "found": False,
        "source_ref": "S01",
        "error": "source_text_storage_temporarily_unavailable",
        "retryable": True,
    }
    assert workspace["source_read_receipts"] == []

    second = await owner.read_source_tool(
        {"source_ref": "S01", "cursor": "", "max_chars": 1_000}
    )

    assert second["found"] is True
    assert _passage_text(second) == text[: second["range"]["end_offset"]]
    assert second["before_opening_different_source"] == {
        "required": True,
        "source_ref": "S01",
        "receipt_refs_from_this_read": [second["receipt_ref"]],
        "allowed_next_actions": ["save_reading_note", "save_source_analysis"],
        "next_action": (
            "Continue this source with next_cursor, or before opening a different source save a reading "
            "note covering this and every earlier uncheckpointed sequential receipt for the source; a "
            "complete source analysis is also valid."
        ),
    }
    keys = list(second)
    assert keys.index("before_opening_different_source") < keys.index("passages")
    assert keys.index("source_analysis_guidance") < keys.index("passages")
    assert second["source_analysis_guidance"]["recommended_next_action"] in {
        "continue_read_source",
        "save_source_analysis",
    }
    assert len(workspace["source_read_receipts"]) == 1


@pytest.mark.asyncio
async def test_read_source_pages_reconstruct_text_and_expose_bounded_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows

    headings = [f"## Heading {index}\n\n{'word ' * 180}\n\n" for index in range(180)]
    text = "".join(headings)
    version = _version()
    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)

    cursor = ""
    reconstructed = ""
    first = True
    while True:
        page = await read_source_text_page(
            SimpleNamespace(db=_Db()),
            version,
            cursor=cursor,
            max_chars=2_000,
        )
        reconstructed += page["text"]
        if first:
            structure = page["structure"]
            assert structure["counts"]["headings"] == 180
            assert structure["total_count"] == 180
            assert 1 <= structure["returned_count"] <= 24
            assert structure["omitted_count"] == 180 - structure["returned_count"]
            assert structure["next_structure_cursor"] == str(structure["returned_count"])
            assert "next_cursor" not in structure
            assert all("preview" not in item and "text" not in item for item in structure["items"])
            assert all("label_preview" in item and "label_chars" in item for item in structure["items"])
            assert all("label_omitted_chars" in item for item in structure["items"])
            assert all("start_offset" in item and "end_offset" in item for item in structure["items"])
            first = False
        else:
            assert "structure" not in page
        if page["eof"]:
            break
        cursor = page["next_cursor"]

    assert reconstructed == text


@pytest.mark.asyncio
async def test_search_source_ranks_exact_phrase_and_returns_exact_paragraph_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows

    text = (
        "Reserve notices appeared repeatedly without an explanation.\n\n"
        "Another reserve notice described routine accounting.\n\n"
        "Mechanism\n\n"
        "The reserve backing mechanism linked certificate issuance to note circulation.\n\n"
        "A final reserve reference discussed later reporting."
    )
    version = _version()
    owner = SimpleNamespace(db=_Db())
    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)

    results = await search_source_text_ranges(
        owner,
        version,
        query="reserve backing mechanism",
        max_results=3,
        max_chars=400,
    )

    assert results
    assert "reserve backing mechanism" in results[0]["text"].lower()
    assert results[0]["boundary_kind"] == "complete_paragraphs"
    selected = resolve_window_ref(results[0]["window_ref"], source_version_id=version.id, text=text)
    assert text[selected.start_offset : selected.end_offset] == results[0]["text"]
    exact_text, _locator, _locator_type, _channel = await read_source_window(
        owner,
        version,
        locator=results[0]["window_ref"],
        max_chars=3_000,
    )
    assert exact_text == results[0]["text"]


@pytest.mark.asyncio
async def test_search_source_does_not_let_frequent_query_words_hide_rare_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows

    common = "policy mechanism market " * 2_000
    target = "Quasar evidence identifies the decisive causal link."
    text = f"{common} {target}"
    version = _version()
    owner = SimpleNamespace(db=_Db())
    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)

    results = await search_source_text_ranges(
        owner,
        version,
        query="policy mechanism market quasar",
        max_results=8,
        max_chars=400,
    )

    assert results
    assert "quasar" in results[0]["text"].lower()


@pytest.mark.asyncio
async def test_search_source_only_expands_a_bounded_shortlist_on_repetitive_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows

    text = ("policy mechanism market " * 20_000) + "quasar final evidence"
    version = _version()
    owner = SimpleNamespace(db=_Db())
    expansion_calls = 0
    original_expand = evidence_local_windows._bounded_completed_context_range

    def counted_expand(*args: Any, **kwargs: Any) -> tuple[int, int, str]:
        nonlocal expansion_calls
        expansion_calls += 1
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_local_windows, "_bounded_completed_context_range", counted_expand)

    results = await search_source_text_ranges(
        owner,
        version,
        query="policy mechanism market quasar",
        max_results=8,
        max_chars=400,
    )

    assert results
    assert "quasar" in results[0]["text"].lower()
    assert expansion_calls <= evidence_local_windows._SEARCH_SHORTLIST_MAX_BLOCKS


@pytest.mark.asyncio
async def test_read_source_receipt_resumes_after_worker_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_local_windows, evidence_source_tools

    text = "".join(f"Paragraph {index}. {'detail ' * 100}\n\n" for index in range(8))
    version = _version()
    run_id = uuid.uuid4()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(
        _db: Any,
        *,
        generation_run_id: Any,
        workspace: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        assert generation_run_id == run_id

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)
    cursor = ""
    reconstructed = ""
    while True:
        owner = _NavigationOwner(version)
        owner.run.id = run_id
        result = await owner.read_source_tool({"source_ref": "S01", "cursor": cursor, "max_chars": 1_000})
        assert result["found"] is True
        assert result["delivery_status"] == "delivered"
        assert "window_ref" not in result
        assert all(
            str(passage["passage_ref"]).startswith("P:S01:")
            for passage in result["passages"]
        )
        reconstructed += _passage_text(result)
        receipt = next(
            item for item in workspace["source_read_receipts"] if item["receipt_ref"] == result["receipt_ref"]
        )
        assert "text" not in receipt
        assert receipt["start_offset"] == result["range"]["start_offset"]
        assert receipt["end_offset"] == result["range"]["end_offset"]
        assert receipt["next_cursor"] == result["next_cursor"]
        assert receipt["eof"] == result["eof"]
        assert receipt["request"] == {"cursor": cursor, "max_chars": 1_000}
        assert [item["passage_ref"] for item in receipt["passage_handles"]] == [
            item["passage_ref"] for item in result["passages"]
        ]
        assert receipt["passage_refs"] == [
            item["canonical_passage_ref"] for item in receipt["passage_handles"]
        ]
        assert receipt["delivery_status"] == "pending"
        receipt["delivery_status"] = "delivered"
        cursor = result["next_cursor"]
        if result["eof"]:
            break

    assert reconstructed == text
    assert len(workspace["source_read_receipts"]) > 1


@pytest.mark.asyncio
async def test_provider_failure_does_not_advance_past_pending_text_range_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2 import source_triage_workspace_prompting
    from app.generation_v2.tools import (
        evidence_local_source,
        evidence_local_windows,
        evidence_source_tools,
    )

    text = "".join(f"Paragraph {index}. {'detail ' * 100}\n\n" for index in range(8))
    version = _version()
    run_id = uuid.uuid4()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [
            {
                "set_id": "set-1",
                "source_refs": ["S01"],
                "question_ids": [],
            }
        ],
        "source_read_receipts": [],
        "coverage_questions": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)

    first_owner = _NavigationOwner(version)
    first_owner.run.id = run_id
    first = await first_owner.read_source_tool(
        {"source_ref": "S01", "cursor": "", "max_chars": 1_000}
    )
    first_receipt = next(
        item
        for item in workspace["source_read_receipts"]
        if item["receipt_ref"] == first["receipt_ref"]
    )
    first_receipt["delivery_status"] = "delivered"

    second = await first_owner.read_source_tool(
        {
            "source_ref": "S01",
            "cursor": first["next_cursor"],
            "max_chars": 1_000,
        }
    )
    second_receipt = next(
        item
        for item in workspace["source_read_receipts"]
        if item["receipt_ref"] == second["receipt_ref"]
    )
    assert second_receipt["delivery_status"] == "pending"

    resumed_payload = source_triage_workspace_prompting.source_set_prompt_payload(
        workspace,
        "set-1",
    )
    assert resumed_payload["saved_state_previews"]["latest_source_read_receipts"][0][
        "receipt_ref"
    ] == first["receipt_ref"]
    assert resumed_payload["saved_state_previews"]["pending_text_reads"] == [
        {
            "receipt_ref": second["receipt_ref"],
            "source_ref": "S01",
            "read_kind": "sequential",
            "start_offset": second["range"]["start_offset"],
            "end_offset": second["range"]["end_offset"],
            "retry": {
                "tool": "read_source",
                "source_ref": "S01",
                "cursor": first["receipt_ref"],
                "max_chars": 1_000,
            },
        }
    ]

    restarted_owner = _NavigationOwner(version)
    restarted_owner.run.id = run_id
    rejected_skip = await restarted_owner.read_source_tool(
        {
            "source_ref": "S01",
            "cursor": second["receipt_ref"],
            "max_chars": 1_000,
        }
    )
    assert rejected_skip["error"] == "source_cursor_not_delivered"

    repeated = await restarted_owner.read_source_tool(
        {
            "source_ref": "S01",
            "cursor": first["receipt_ref"],
            "max_chars": 1_000,
        }
    )
    assert repeated["passages"] == second["passages"]
    assert repeated["range"] == second["range"]
    assert repeated["receipt_ref"] == second["receipt_ref"]


@pytest.mark.asyncio
async def test_delivered_passage_materializes_exact_evidence_after_worker_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_local_windows, evidence_source_tools

    exact_quote = "The unique causal mechanism linked reserve assets to certificate issuance."
    text = f"Opening context.\n\n{exact_quote}\n\nLater limitations and qualifications."
    version = _version()
    run_id = uuid.uuid4()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
        "intent": {"language": "ru"},
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    persisted_snippets: list[str] = []

    async def persist_evidence(*_args: Any, **kwargs: Any) -> Any:
        persisted_snippets.append(kwargs["snippet_text"])
        return SimpleNamespace(
            id=uuid.uuid4(),
            snippet_text=kwargs["snippet_text"],
            locator_text=kwargs["locator_text"],
        )

    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)
    monkeypatch.setattr(evidence_source_tools, "persist_evidence_item", persist_evidence)

    first_owner = _NavigationOwner(version)
    first_owner.run.id = run_id
    read = await first_owner.read_source_tool({"source_ref": "S01", "cursor": "", "max_chars": 1_000})
    assert read["found"] is True
    read_receipt = next(
        item
        for item in workspace["source_read_receipts"]
        if item["receipt_ref"] == read["receipt_ref"]
    )

    pending_owner = _NavigationOwner(version)
    pending_owner.run.id = run_id
    passage = read["passages"][0]
    pending_capture = await pending_owner.materialize_source_passages(
        source_ref="S01",
        passage_refs=[passage["passage_ref"]],
    )
    assert pending_capture["error"] == "passage_ref_not_delivered_to_current_set_agent"

    read_receipt["delivery_status"] = "delivered"

    restarted_owner = _NavigationOwner(version)
    restarted_owner.run.id = run_id

    async def unexpected_checkpoint(**_kwargs: Any) -> None:
        raise AssertionError("evidence selection from delivered text is not a new source read")

    monkeypatch.setattr(restarted_owner, "_triage_read_guard_error", unexpected_checkpoint)
    monkeypatch.setattr(restarted_owner, "_record_triage_exact_read", unexpected_checkpoint)
    invalid_capture = await restarted_owner.materialize_source_passages(
        source_ref="S01",
        passage_refs=[passage["passage_ref"] + "x"],
    )

    assert invalid_capture["error"] == "passage_ref_not_delivered_to_current_set_agent"
    assert invalid_capture["passage_ref_corrections"] == [
        {
            "invalid_passage_ref": passage["passage_ref"] + "x",
            "replacement_candidates": [passage["passage_ref"]],
        }
    ]
    assert invalid_capture["retry_policy"] == {
        "preserve_all_other_analysis_fields": True,
        "replace_only_invalid_passage_refs": True,
        "do_not_search_again": True,
    }
    assert invalid_capture["output_language"] == "ru"

    captured = await restarted_owner.materialize_source_passages(
        source_ref="S01",
        passage_refs=[passage["passage_ref"]],
    )

    assert captured["accepted"] is True
    assert len(captured["evidence_ids"]) == 1
    legacy_capture = await restarted_owner.materialize_source_passages(
        source_ref="S01",
        passage_refs=[read_receipt["passage_refs"][0]],
    )
    assert legacy_capture["accepted"] is True
    assert persisted_snippets == [passage["text"], passage["text"]]


@pytest.mark.asyncio
async def test_search_source_receipt_keeps_targeted_evidence_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source, evidence_local_windows, evidence_source_tools

    text = (
        "Earlier context. " * 180
        + "\n\nTarget policy mechanism appears in this complete paragraph.\n\n"
        + "Later context. " * 180
    )
    version = _version()
    run_id = uuid.uuid4()
    workspace: dict[str, Any] = {
        "current_set": {"set_id": "set-1"},
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_read_receipts": [],
    }

    async def load_workspace(_db: Any, _run_id: Any) -> dict[str, Any]:
        return workspace

    async def save_workspace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def save_trace(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def persist_evidence(*_args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            id=uuid.uuid4(),
            snippet_text=kwargs["snippet_text"],
            locator_text=kwargs["locator_text"],
        )

    monkeypatch.setattr(evidence_local_windows, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_local_source, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(evidence_source_tools, "load_source_triage_workspace", load_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_source_triage_workspace", save_workspace)
    monkeypatch.setattr(evidence_source_tools, "save_tool_trace", save_trace)
    monkeypatch.setattr(evidence_source_tools, "persist_evidence_item", persist_evidence)

    owner = _NavigationOwner(version)
    owner.run.id = run_id
    result = await owner.search_source_tool(
        {
            "source_ref": "S01",
            "query": "target policy mechanism",
            "max_results": 2,
            "max_chars": 400,
        }
    )

    assert result["found"] is True
    assert result["delivery_status"] == "delivered"
    first = result["results"][0]
    receipt = next(item for item in workspace["source_read_receipts"] if item["receipt_ref"] == first["receipt_ref"])
    assert receipt["read_kind"] == "search"
    assert receipt["request"] == {
        "query": "target policy mechanism",
        "max_results": 2,
        "max_chars": 400,
    }
    assert receipt["delivery_status"] == "pending"
    assert first["navigation_only"] is True
    assert first["match_text"]
    assert "passages" not in first
    assert "passage_ref" not in first
    assert first["exact_read_call"] == {
        "tool": "read_source_window",
        "arguments": {
            "source_ref": "S01",
            "window_ref": first["window_ref"],
            "locator": "",
            "max_chars": 3_000,
        },
    }
    assert first["context_range"]["start_offset"] <= first["range"]["start_offset"]
    assert first["context_range"]["end_offset"] >= first["range"]["end_offset"]
    assert first["context_range"] != first["range"]
    receipt["delivery_status"] = "delivered"
    exact_read_calls: list[dict[str, Any]] = []

    async def record_exact_read(**kwargs: Any) -> None:
        exact_read_calls.append(kwargs)

    monkeypatch.setattr(owner, "_record_triage_exact_read", record_exact_read)
    assert first["window_ref"].startswith("W:S01:")
    canonical_window_ref = resolve_model_window_ref(owner, first["window_ref"])
    assert canonical_window_ref is not None
    assert canonical_window_ref != receipt["window_ref"]
    selected = resolve_window_ref(canonical_window_ref, source_version_id=version.id, text=text)
    assert selected.start_offset == first["context_range"]["start_offset"]
    assert selected.end_offset == first["context_range"]["end_offset"]
    captured = await owner.read_source_window_tool(
        first["exact_read_call"]["arguments"]
    )
    assert captured["found"] is True
    assert "receipt_ref" not in captured
    context_range = first["context_range"]
    assert captured["evidence"]["snippet_text"] == text[
        context_range["start_offset"] : context_range["end_offset"]
    ]
    assert exact_read_calls == [
        {
            "source_ref": "S01",
            "evidence_ids": [captured["evidence"]["evidence_id"]],
        }
    ]
