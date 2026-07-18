from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.tools.evidence_local_windows import (
    read_source_window,
    sequential_source_window_page,
    source_allows_timestamp_locators,
    source_windows,
)
from app.generation_v2.tools.evidence_exact_read import (
    evidence_has_current_exact_read,
    mark_evidence_exact_read,
)
from app.generation_v2.tools.evidence_model_window_refs import (
    register_model_window_ref,
    resolve_model_window_ref,
)
from app.generation_v2.tools.evidence_store import canonical_evidence_hash, persist_evidence_item
from app.generation_v2.tools.evidence_text_windows import (
    representative_text_windows,
    resolve_window_for_locator,
    source_outline,
)
from app.generation_v2.tools.evidence_window_refs import WindowRefError, create_window_ref, parse_window_ref
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalEvent,
    EvidenceStrength,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None


class _PersistenceDb:
    def __init__(self) -> None:
        self.info: dict = {}
        self.added: list[object] = []

    async def get(self, _model, _item_id):
        return None

    async def execute(self, _statement):
        return _ScalarResult([item for item in self.added if isinstance(item, EvidenceItem)])

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()


class _SourceDb:
    async def get(self, _model, _item_id):
        return None


class _TypedSourceDb:
    def __init__(self, source_type: str) -> None:
        self.source_type = source_type

    async def get(self, _model, _item_id):
        return SimpleNamespace(source_type=self.source_type)


def _version(*, kind: str) -> SourceVersion:
    return SourceVersion(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind=kind,
        storage_path="source.txt",
        status=V2RecordStatus.READY,
        upload_status=V2RecordStatus.READY,
        metadata_={},
    )


def test_source_outline_treats_timestamps_as_structure_only_when_enabled() -> None:
    text = "Article title\n\nAt 11:30 the market moved\n\nWhat happened next\n\nThe result."

    document_outline = source_outline(text, max_items=4, allow_timestamps=False)
    transcript_outline = source_outline(text, max_items=4, allow_timestamps=True)

    assert all(item["locator_type"] != EvidenceLocatorType.TIMESTAMP for item in document_outline)
    assert transcript_outline[0]["locator_type"] == EvidenceLocatorType.TIMESTAMP
    assert resolve_window_for_locator(text, locator="11:30", max_chars=500, allow_timestamps=False) is None
    timed_heading = resolve_window_for_locator(
        text,
        locator="At 11:30 the market moved",
        max_chars=500,
        allow_timestamps=False,
    )
    assert timed_heading is not None
    assert timed_heading.locator_type == EvidenceLocatorType.SECTION


@pytest.mark.asyncio
async def test_audio_and_video_sources_enable_timestamp_locators_for_text_representations() -> None:
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)

    assert await source_allows_timestamp_locators(SimpleNamespace(db=_TypedSourceDb("audio")), version) is True
    assert await source_allows_timestamp_locators(SimpleNamespace(db=_TypedSourceDb("video")), version) is True
    assert await source_allows_timestamp_locators(SimpleNamespace(db=_TypedSourceDb("web")), version) is False


def test_unresolved_locator_never_falls_back_to_source_beginning() -> None:
    text = "Heading\n\nActual source content."

    assert resolve_window_for_locator(text, locator="missing section", max_chars=500) is None
    beginning = resolve_window_for_locator(text, locator="source beginning", max_chars=500)
    assert beginning is not None
    assert beginning.locator_type == EvidenceLocatorType.SOURCE_LEVEL


def test_unstructured_skim_samples_across_a_long_single_paragraph() -> None:
    text = " ".join(f"token-{idx}" for idx in range(1200))

    windows = representative_text_windows(text, max_windows=4, max_chars=1200)

    assert len(windows) == 4
    assert windows[0].start_offset == 0
    assert windows[-1].end_offset == len(text)
    assert len({window.start_offset for window in windows}) == 4


@pytest.mark.asyncio
async def test_structural_outline_does_not_hide_broad_source_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows as windows_module

    prefix = " ".join(f"opening-{idx}" for idx in range(900))
    text = f"{prefix}\n\nAppendix\n\nThe late appendix material."
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)

    windows = await source_windows(SimpleNamespace(db=_SourceDb()), version, max_windows=4)
    starts = [parse_window_ref(item["window_ref"]).start_offset for item in windows]
    ends = [parse_window_ref(item["window_ref"]).end_offset for item in windows]

    assert starts[0] == 0
    assert max(ends) == len(text)
    assert any(item["locator_text"] == "Appendix" for item in windows)


@pytest.mark.asyncio
async def test_sequential_source_pages_reconstruct_long_single_section_losslessly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_windows as windows_module

    text = "".join(f"segment-{idx:04d}|" for idx in range(1800))
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    owner = SimpleNamespace(db=_SourceDb())
    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)

    cursor = ""
    reconstructed = ""
    while True:
        page = await sequential_source_window_page(
            owner,
            version,
            cursor=cursor,
            max_windows=3,
            max_chars=700,
        )
        reconstructed += "".join(item["snippet_text"] for item in page["windows"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    assert reconstructed == text


@pytest.mark.asyncio
async def test_inspect_source_exposes_contiguous_continuation_until_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source as source_module
    from app.generation_v2.tools import evidence_local_windows as windows_module
    from app.generation_v2.tools.evidence import EvidenceToolset

    text = "".join(f"part-{idx:04d}|" for idx in range(1000))
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_ref(_source_id):
        return "S01"

    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(source_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_error)
    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)

    cursor = ""
    previous_end = 0
    while True:
        result = await toolset.inspect_source_tool(
            {"source_ref": "S01", "max_items": 2, "cursor": cursor}
        )
        assert result["found"] is True
        assert result["page_start"] == previous_end
        previous_end = result["page_end"]
        cursor = result["next_cursor"]
        if not cursor:
            break

    assert previous_end == len(text)


@pytest.mark.asyncio
async def test_invalid_window_handle_returns_a_concrete_working_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_local_source as source_module
    from app.generation_v2.tools import evidence_local_windows as windows_module
    from app.generation_v2.tools.evidence import EvidenceToolset

    text = "Concrete readable source text."
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_ref(_source_id):
        return "S01"

    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(source_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)

    result = await toolset.read_source_window_tool(
        {
            "source_ref": "S01",
            "window_ref": "W:S01:invented",
            "locator": "",
            "max_chars": 1000,
        }
    )

    assert result["found"] is False
    assert result["error"] == "invalid_window_ref"
    available = result["available_window_ref"]
    canonical = resolve_model_window_ref(toolset, available)
    assert canonical is not None
    assert parse_window_ref(canonical).source_version_id == version.id


@pytest.mark.asyncio
async def test_inspect_source_caps_requested_page_to_model_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.runtime_tool_output import model_facing_tool_output
    from app.generation_v2.tools import evidence_local_source as source_module
    from app.generation_v2.tools import evidence_local_windows as windows_module
    from app.generation_v2.tools.evidence import EvidenceToolset

    heading = "H" * 110
    chunk = f"{heading}\n" + "x" * (1600 - len(heading) - 1)
    text = chunk * 40
    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_ref(_source_id):
        return "S01"

    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(source_module, "_source_version_text", lambda _version: text)
    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_error)
    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)

    result = await toolset.inspect_source_tool(
        {"source_ref": "S01", "max_items": 40, "cursor": ""}
    )

    assert len(result["items"]) == 24
    assert result["next_cursor"]
    assert model_facing_tool_output("inspect_source", result) == result


@pytest.mark.asyncio
async def test_read_source_window_guard_uses_resolved_source_when_input_ref_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    text = "Exact source window."
    canonical_ref = create_window_ref(
        source_version_id=version.id,
        text=text,
        start_offset=0,
        end_offset=len(text),
    )
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
        ),
        provider_index=None,
    )
    handle = register_model_window_ref(toolset, source_ref="S02", canonical_ref=canonical_ref)
    guarded: list[dict] = []

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_ref(_source_id):
        return "S02"

    async def guard(**kwargs):
        guarded.append(kwargs)
        return {"outcome_kind": "control", "control_code": "blocked"}

    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_triage_read_guard_error", guard)

    result = await toolset.read_source_window_tool(
        {
            "source_ref": "",
            "window_ref": handle,
            "locator": "",
            "max_chars": 1000,
        }
    )

    assert result["control_code"] == "blocked"
    assert guarded == [{"target_source_refs": ["S02"], "exact_read": True}]


@pytest.mark.asyncio
async def test_stale_signed_window_ref_returns_current_working_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    current = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    stale_version_id = uuid.uuid4()
    stale_text = "Old source representation."
    current_text = "Current readable source representation."
    stale_ref = create_window_ref(
        source_version_id=stale_version_id,
        text=stale_text,
        start_offset=0,
        end_offset=len(stale_text),
    )
    current_ref = create_window_ref(
        source_version_id=current.id,
        text=current_text,
        start_offset=0,
        end_offset=len(current_text),
    )
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=current.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(current.source_id)],
            selected_source_version_ids=[str(current.id)],
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**kwargs):
        if kwargs.get("source_version_id"):
            return None
        return current

    async def source_ref(_source_id):
        return "S01"

    async def current_page(_version, **_kwargs):
        return {
            "windows": [{"window_ref": current_ref}],
            "next_cursor": "",
            "total_chars": len(current_text),
            "page_start": 0,
            "page_end": len(current_text),
        }

    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_sequential_source_window_page", current_page)

    result = await toolset.read_source_window_tool(
        {
            "source_ref": "S01",
            "window_ref": stale_ref,
            "locator": "",
            "max_chars": 1000,
        }
    )

    assert result["found"] is False
    assert result["error"] == "window_ref_invalid_or_stale"
    available = result["available_window_ref"]
    assert resolve_model_window_ref(toolset, available) == current_ref


@pytest.mark.asyncio
async def test_window_ref_round_trips_the_same_transcript_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_local_windows as windows_module

    text = "[00:00] Intro\nThe opening.\n\n[12:30] Mechanism\nThe exact mechanism.\n\n[25:00] Ending\nThe end."
    version = _version(kind=SourceRepresentationKind.TRANSCRIPT)
    owner = SimpleNamespace(db=_SourceDb())
    monkeypatch.setattr(windows_module, "_source_version_text", lambda _version: text)

    skimmed = await source_windows(owner, version, max_windows=3)
    selected = skimmed[1]
    snippet, locator_text, locator_type, _channel = await read_source_window(
        owner,
        version,
        locator=selected["window_ref"],
        max_chars=3000,
    )

    assert snippet == selected["snippet_text"]
    assert locator_text == selected["locator_text"]
    assert locator_type == EvidenceLocatorType.TIMESTAMP


def test_window_ref_rejects_tampering() -> None:
    version_id = uuid.uuid4()
    text = "A concrete source window."
    window_ref = create_window_ref(
        source_version_id=version_id,
        text=text,
        start_offset=0,
        end_offset=len(text),
    )

    assert parse_window_ref(window_ref).source_version_id == version_id
    with pytest.raises(WindowRefError, match="invalid_window_ref"):
        parse_window_ref(f"{window_ref[:-1]}x")


def test_exact_evidence_hash_distinguishes_repeated_text_by_verified_window() -> None:
    version_id = uuid.uuid4()
    text = "Repeated statement. Middle. Repeated statement."
    first_start = text.index("Repeated statement.")
    second_start = text.rindex("Repeated statement.")
    first_ref = create_window_ref(
        source_version_id=version_id,
        text=text,
        start_offset=first_start,
        end_offset=first_start + len("Repeated statement."),
    )
    second_ref = create_window_ref(
        source_version_id=version_id,
        text=text,
        start_offset=second_start,
        end_offset=second_start + len("Repeated statement."),
    )
    common = {
        "snippet_text": "Repeated statement.",
        "normalized_quote_text": "repeated statement.",
        "locator_text": "same section",
        "locator_type": EvidenceLocatorType.SECTION,
        "evidence_strength": EvidenceStrength.EXACT,
    }

    first_hash = canonical_evidence_hash(**common, metadata={"window_ref": first_ref})
    second_hash = canonical_evidence_hash(**common, metadata={"window_ref": second_ref})
    snippet_hash = canonical_evidence_hash(
        **{**common, "evidence_strength": EvidenceStrength.SNIPPET},
        metadata={"window_ref": first_ref},
    )
    same_snippet_hash = canonical_evidence_hash(
        **{**common, "evidence_strength": EvidenceStrength.SNIPPET},
        metadata={"window_ref": second_ref},
    )

    assert first_hash != second_hash
    assert snippet_hash == same_snippet_hash


@pytest.mark.asyncio
async def test_same_canonical_evidence_has_separate_retrieval_events() -> None:
    db = _PersistenceDb()
    run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    source_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    common = {
        "generation_run_id": run_id,
        "project_id": project_id,
        "source_id": source_id,
        "source_version_id": source_version_id,
        "locator_text": "section 1",
    }

    first = await persist_evidence_item(
        db,  # type: ignore[arg-type]
        **common,
        retrieval_channel="provider_vector_search",
        query_text="first query",
        purpose="mechanism",
        snippet_text="Same   canonical evidence.",
    )
    second = await persist_evidence_item(
        db,  # type: ignore[arg-type]
        **common,
        retrieval_channel="local_chunk_search",
        query_text="different query",
        purpose="timeline",
        snippet_text="Same canonical evidence.",
    )

    evidence = [item for item in db.added if isinstance(item, EvidenceItem)]
    events = [item for item in db.added if isinstance(item, EvidenceRetrievalEvent)]
    assert first is second
    assert len(evidence) == 1
    assert len(events) == 2
    assert {event.query_text for event in events} == {"first query", "different query"}
    assert {event.purpose for event in events} == {"mechanism", "timeline"}
    assert {event.retrieval_channel for event in events} == {"provider_vector_search", "local_chunk_search"}


@pytest.mark.asyncio
async def test_skim_returns_window_refs_without_persisting_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_source_tool_actions as actions

    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def windows(_version, *, max_windows: int):
        return [
            {
                "window_ref": f"w1.test-{idx}",
                "locator_text": f"section {idx}",
                "locator_type": EvidenceLocatorType.SECTION,
                "snippet_text": f"Preview {idx}",
                "channel": "local_chunk_search",
            }
            for idx in range(max_windows)
        ]

    async def source_ref(_source_id):
        return "S01"

    async def version_ref(_version):
        return "S01:v1"

    async def save_trace(*_args, **_kwargs):
        return None

    async def fail_persist(*_args, **_kwargs):
        raise AssertionError("skim must not create EvidenceItems")

    owner = SimpleNamespace(
        db=SimpleNamespace(),
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        _triage_read_guard_error=no_error,
        _invalid_source_or_version_ref_error=no_error,
        _resolve_source_version=resolve,
        _source_windows=windows,
        _source_ref_for_id=source_ref,
        _version_ref_for_version=version_ref,
    )
    monkeypatch.setattr(actions, "save_tool_trace", save_trace)
    monkeypatch.setattr(actions, "persist_evidence_item", fail_persist)

    result = await actions.skim_source(owner, {"source_ref": "S01", "max_windows": 3})

    assert result["found"] is True
    assert result["support_status"] == "preview_only"
    assert result["exact_read_next_action"] == "read_source_window"
    assert len(result["windows"]) == 3
    assert all("window_ref" in item for item in result["windows"])
    assert [item["window_ref"] for item in result["windows"]] == [
        "W:S01:1",
        "W:S01:2",
        "W:S01:3",
    ]
    assert "version_ref" not in result
    assert all("evidence" not in item for item in result["windows"])
    from app.generation_v2.runtime_tool_output import model_facing_tool_output

    assert model_facing_tool_output("skim_source", result) == result


def test_short_model_window_refs_are_stable_and_reject_invented_handles() -> None:
    owner = SimpleNamespace()
    canonical = "w1.signed-canonical-window"

    first = register_model_window_ref(owner, source_ref="S07", canonical_ref=canonical)
    second = register_model_window_ref(owner, source_ref="S07", canonical_ref=canonical)

    assert first == second == "W:S07:1"
    assert resolve_model_window_ref(owner, first) == canonical
    assert resolve_model_window_ref(owner, "W:S07:2") is None


@pytest.mark.asyncio
async def test_exact_read_provenance_is_durable_and_attempt_scoped() -> None:
    db = _PersistenceDb()
    run_id = uuid.uuid4()
    version_id = uuid.uuid4()
    evidence = SimpleNamespace(
        id=uuid.uuid4(),
        source_version_id=version_id,
        snippet_text="Exact persisted passage.",
        metadata_={"source_internal_search": True},
    )
    owner = SimpleNamespace(
        db=db,
        run=SimpleNamespace(id=run_id),
        run_view=SimpleNamespace(revision=3, attempt=2),
    )

    assert evidence_has_current_exact_read(owner, evidence) is False
    assert await mark_evidence_exact_read(owner, evidence, method="read_evidence") is True
    provenance = evidence.metadata_["exact_read_provenance"]
    assert provenance == {
        "generation_run_id": str(run_id),
        "corpus_revision": 3,
        "attempt": 2,
        "evidence_id": str(evidence.id),
        "source_version_id": str(version_id),
        "snippet_fingerprint": hashlib.sha256(evidence.snippet_text.encode()).hexdigest(),
        "method": "read_evidence",
    }

    owner.run_view = SimpleNamespace(revision=3, attempt=3)
    assert evidence_has_current_exact_read(owner, evidence) is False


@pytest.mark.asyncio
async def test_long_evidence_requires_every_stable_page_before_exact_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    evidence_id = uuid.uuid4()
    text = "".join(f"evidence-{idx:04d}|" for idx in range(500))
    evidence = SimpleNamespace(
        id=evidence_id,
        source_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        snippet_text=text,
        metadata_={"source_internal_search": True},
    )
    toolset = EvidenceToolset(
        db=_PersistenceDb(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(revision=2, attempt=4),
        provider_index=None,
    )

    async def evidence_in_run(_evidence_id: str):
        return evidence

    async def no_scope(_source_ids: list[str]):
        return None

    async def source_ref(_source_id):
        return "S01"

    async def page_payload(_item, *, start_offset: int, end_offset: int):
        return {
            "evidence_id": str(evidence_id),
            "snippet_text": text[start_offset:end_offset],
            "snippet_start": start_offset,
            "snippet_end": end_offset,
        }

    async def no_guard(*_args, **_kwargs):
        return None

    monkeypatch.setattr(toolset, "_evidence_in_run", evidence_in_run)
    monkeypatch.setattr(toolset, "_triage_scope_error_for_source_ids", no_scope)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_evidence_detail_page_payload", page_payload)
    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_guard)

    page_ref = ""
    reconstructed = ""
    page_count = 0
    while True:
        result = await toolset.read_evidence_tool(
            {
                "evidence_id": str(evidence_id),
                "page_ref": page_ref,
                "max_chars": 1000,
            }
        )
        assert result["found"] is True
        reconstructed += result["evidence"]["snippet_text"]
        page_count += 1
        page_ref = result["next_page_ref"]
        if not page_ref:
            assert result["exact_read_complete"] is True
            assert "next_action" not in result
            break
        assert result["exact_read_complete"] is False
        assert "next_page_ref as page_ref" in result["next_action"]
        assert toolset._evidence_has_current_exact_read(evidence) is False

    assert page_count > 1
    assert reconstructed == text
    assert toolset._evidence_has_current_exact_read(evidence) is True


@pytest.mark.asyncio
async def test_successful_model_read_records_pending_source_before_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4()),
        run_view=SimpleNamespace(revision=1, attempt=1),
        provider_index=None,
    )
    calls: list[dict] = []

    async def record(**kwargs):
        calls.append(kwargs)
        return {"pending_source_refs": [kwargs["source_ref"]]}

    monkeypatch.setattr(toolset, "_record_triage_exact_read", record)

    failed = await toolset._finalize_model_exact_read(
        {"found": False, "coverage_warning": "No readable evidence."},
        requested_source_ref="S01",
    )
    accepted = await toolset._finalize_model_exact_read(
        {"found": True, "evidence": {"evidence_id": "E01", "source_ref": "S01"}},
        requested_source_ref="S01",
    )

    assert "exact_read_control" not in failed
    assert calls == [{"source_ref": "S01", "evidence_ids": ["E01"]}]
    assert accepted["exact_read_control"]["pending_source_refs"] == ["S01"]


@pytest.mark.asyncio
async def test_short_model_window_ref_round_trips_without_model_visible_version_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation_v2.tools import evidence_source_tools as source_tools_module
    from app.generation_v2.tools.evidence import EvidenceToolset

    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    text = "Exact source window for the model handle."
    canonical_ref = create_window_ref(
        source_version_id=version.id,
        text=text,
        start_offset=0,
        end_offset=len(text),
    )
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)],
            selected_source_version_ids=[str(version.id)],
        ),
        provider_index=None,
    )
    handle = register_model_window_ref(toolset, source_ref="S01", canonical_ref=canonical_ref)
    captured: dict[str, object] = {}

    async def no_guard(*_args, **_kwargs):
        return None

    async def validate_refs(**kwargs):
        captured["validated_version_ref"] = kwargs["version_ref"]
        return None

    async def resolve_version(**kwargs):
        captured["resolved_version_ref"] = kwargs["version_ref"]
        captured["resolved_source_version_id"] = kwargs["source_version_id"]
        return version

    async def source_id(_value: str):
        return str(version.source_id)

    async def source_ref(_source_id):
        return "S01"

    async def read_window(_version, *, locator: str, max_chars: int):
        captured["canonical_locator"] = locator
        return text, "section 1", EvidenceLocatorType.SECTION, "local_chunk_search"

    async def persist(_db, **kwargs):
        captured["persist"] = kwargs
        return SimpleNamespace(id=uuid.uuid4())

    async def detail(_evidence, **_kwargs):
        return {"evidence_id": "E01", "locator_text": "section 1"}

    async def save_trace(*_args, **_kwargs):
        return None

    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_guard)
    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", validate_refs)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve_version)
    monkeypatch.setattr(toolset, "_source_id_from_ref_or_id", source_id)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_read_source_window", read_window)
    monkeypatch.setattr(toolset, "_evidence_detail_payload", detail)
    monkeypatch.setattr(source_tools_module, "persist_evidence_item", persist)
    monkeypatch.setattr(source_tools_module, "save_tool_trace", save_trace)

    result = await toolset.read_source_window_tool(
        {
            "source_ref": "S01",
            "window_ref": handle,
            "locator": "",
            "max_chars": 1000,
        }
    )

    assert result["found"] is True
    assert result["window_ref"] == handle
    assert captured["validated_version_ref"] == ""
    assert captured["resolved_version_ref"] == ""
    assert captured["resolved_source_version_id"] == str(version.id)
    assert captured["canonical_locator"] == canonical_ref
    persisted = captured["persist"]
    assert isinstance(persisted, dict)
    assert persisted["metadata"]["window_ref"] == canonical_ref


@pytest.mark.asyncio
async def test_read_source_window_reports_locator_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools.evidence import EvidenceToolset

    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    toolset = EvidenceToolset(
        db=None,  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)], selected_source_version_ids=[str(version.id)]
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_id(_value: str):
        return str(version.source_id)

    async def source_ref(_source_id):
        return "S01"

    async def no_window(*_args, **_kwargs):
        return "", "", EvidenceLocatorType.UNKNOWN, "local_chunk_search"

    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_error)
    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_id_from_ref_or_id", source_id)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_read_source_window", no_window)
    monkeypatch.setattr(toolset, "_cached_source_version_text", lambda _version: "readable source text")

    result = await toolset.read_source_window_tool(
        {"source_ref": "S01", "locator": "missing section", "max_chars": 1000}
    )

    assert result["found"] is False
    assert result["error"] == "locator_not_found"


@pytest.mark.asyncio
async def test_source_beginning_is_not_marked_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.generation_v2.tools import evidence_source_tools as source_tools_module
    from app.generation_v2.tools.evidence import EvidenceToolset

    version = _version(kind=SourceRepresentationKind.EXTRACTED_TEXT)
    captured: dict[str, object] = {}
    toolset = EvidenceToolset(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        run=SimpleNamespace(id=uuid.uuid4(), project_id=version.project_id),
        run_view=SimpleNamespace(
            selected_source_ids=[str(version.source_id)], selected_source_version_ids=[str(version.id)]
        ),
        provider_index=None,
    )

    async def no_error(*_args, **_kwargs):
        return None

    async def resolve(**_kwargs):
        return version

    async def source_id(_value: str):
        return str(version.source_id)

    async def source_ref(_source_id):
        return "S01"

    async def beginning(*_args, **_kwargs):
        return "Beginning text.", "source beginning", EvidenceLocatorType.SOURCE_LEVEL, "local_chunk_search"

    async def persist(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def detail(_evidence, **_kwargs):
        return {"evidence_id": "E01", "locator_text": "source beginning"}

    async def save_trace(*_args, **_kwargs):
        return None

    monkeypatch.setattr(toolset, "_triage_read_guard_error", no_error)
    monkeypatch.setattr(toolset, "_invalid_source_or_version_ref_error", no_error)
    monkeypatch.setattr(toolset, "_resolve_source_version", resolve)
    monkeypatch.setattr(toolset, "_source_id_from_ref_or_id", source_id)
    monkeypatch.setattr(toolset, "_source_ref_for_id", source_ref)
    monkeypatch.setattr(toolset, "_read_source_window", beginning)
    monkeypatch.setattr(toolset, "_evidence_detail_payload", detail)
    monkeypatch.setattr(source_tools_module, "persist_evidence_item", persist)
    monkeypatch.setattr(source_tools_module, "save_tool_trace", save_trace)

    result = await toolset.read_source_window_tool(
        {"source_ref": "S01", "locator": "beginning", "max_chars": 1000}
    )

    assert result["found"] is True
    assert captured["evidence_strength"] == EvidenceStrength.SNIPPET
