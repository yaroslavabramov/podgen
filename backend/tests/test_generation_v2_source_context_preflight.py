from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.generation_v2.agents.source_agents import build_source_triage_planner_agent
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceCard, SourceMap, SourceProfile, SourceTriageCompletion
from app.generation_v2.source_triage_workspace_capsules import build_capsule_for_card
from app.generation_v2.source_triage_workspace_prompting import (
    planner_source_catalog,
    source_set_prompt_payload,
)
from app.generation_v2.tools.evidence_payloads import EvidencePayloadMixin
from app.generation_v2.tools.evidence_refs import build_source_ref_context
from app.models.generation_v2 import SourceVersion, V2RecordStatus


def _catalog_inventory(source_count: int) -> tuple[SourceMap, list[str]]:
    internal_ids: list[str] = []
    cards: list[SourceCard] = []
    for index in range(source_count):
        source_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        profile_id = str(uuid.uuid4())
        internal_ids.extend([source_id, version_id, profile_id])
        cards.append(
            SourceCard(
                source_id=source_id,
                source_version_ids=[version_id],
                representation_ids=[version_id],
                source_profile_id=profile_id,
                title=f"Source {index}: {source_id} " + "title " * 40,
                domain=f"reports-{version_id}.example.test",
                source_type="web",
                published_at=None,
                coverage_status="ready",
                summary=(
                    f"[source_id={source_id} page_marker=1 paragraph=1] "
                    f"Neutral orientation for {version_id}. "
                    + "summary " * 80
                ),
                gaps_or_cautions=[
                    "Publication date is missing.",
                    f"source window {index + 1}",
                    f"Concrete caution for {profile_id}. " + "detail " * 40,
                ],
            )
        )
    return SourceMap(source_cards=cards), internal_ids


def test_planner_catalog_is_complete_bounded_and_has_honest_previews_for_100_sources() -> None:
    inventory, internal_ids = _catalog_inventory(100)

    catalog = planner_source_catalog(inventory)
    encoded = json.dumps(catalog, ensure_ascii=False)

    assert catalog["complete_source_list"] is True
    assert catalog["source_count"] == 100
    assert [item["source_ref"] for item in catalog["sources"]] == [
        f"S{index:03d}" for index in range(1, 101)
    ]
    assert len(encoded) < 90_000
    assert "source_id=" not in encoded
    assert "source window" not in encoded.casefold()
    assert all(internal_id not in encoded for internal_id in internal_ids)
    assert all(item["title_omitted_chars"] > 0 for item in catalog["sources"])
    assert all(item["summary_omitted_chars"] > 0 for item in catalog["sources"])
    assert catalog["source_refs_by_metadata_flag"] == {
        "missing_publication_date": [f"S{index:03d}" for index in range(1, 101)]
    }
    assert all(item["caution_previews"][0]["omitted_chars"] > 0 for item in catalog["sources"])


@pytest.mark.asyncio
async def test_full_36_source_planner_preflight_needs_no_capsule_sweep() -> None:
    inventory, internal_ids = _catalog_inventory(36)
    catalog = planner_source_catalog(inventory)
    refs = [item["source_ref"] for item in catalog["sources"]]
    tool_calls: list[tuple[str, dict]] = []

    async def _handler(name: str, args: dict) -> dict:
        tool_calls.append((name, args))
        if name == "list_source_capsules":
            raise AssertionError("compact catalog should be enough for this planner pass")
        return {"accepted": True}

    def _tool(name: str) -> AgentTool:
        async def handler(args: dict) -> dict:
            return await _handler(name, args)

        return AgentTool(name=name, description=name, parameters={"type": "object"}, handler=handler)

    tools = [
        _tool("list_source_capsules"),
        _tool("route_sources_for_initial_reading"),
        _tool("list_triage_source_sets"),
        _tool("upsert_coverage_question"),
        _tool("upsert_triage_source_set"),
        _tool("finalize_triage_workspace_plan"),
    ]

    class _FakePlannerRuntime:
        async def call_structured(self, **kwargs):
            prompt = kwargs["prompt"]
            encoded_prompt = prompt.encode("utf-8")
            assert len(encoded_prompt) < 35_000
            assert all(ref in prompt for ref in refs)
            assert all(internal_id not in prompt for internal_id in internal_ids)
            assert "source_id=" not in prompt
            assert "source window" not in prompt.casefold()
            by_name = {tool.name: tool for tool in kwargs["local_tools"]}
            await by_name["upsert_coverage_question"].handler(
                {
                    "question_id": "q-main",
                    "question": "What does this corpus establish?",
                    "kind": "mechanism",
                    "importance": "critical",
                    "rationale": "Organizes the complete corpus.",
                    "parent_question_id": "",
                }
            )
            for set_index, start in enumerate(range(0, len(refs), 6), start=1):
                await by_name["upsert_triage_source_set"].handler(
                    {
                        "set_id": f"set-{set_index}",
                        "label": f"Comparison set {set_index}",
                        "purpose": "Read and compare the assigned sources.",
                        "source_refs": refs[start : start + 6],
                        "rationale": "A bounded comparison group.",
                        "question_ids": ["q-main"],
                        "priority": "normal",
                    }
                )
            await by_name["finalize_triage_workspace_plan"].handler(
                {"summary": "Every source is assigned once.", "central_question_id": "q-main"}
            )
            return SourceTriageCompletion(ready=True, summary="planned", notes=[])

    completion = await build_source_triage_planner_agent(
        None,  # type: ignore[arg-type]
        SimpleNamespace(id=uuid.uuid4(), config={"user_prompt": "Explain the corpus"}),  # type: ignore[arg-type]
        _FakePlannerRuntime(),  # type: ignore[arg-type]
        workspace_summary={"status": "orientation", "source_count": 36},
        source_catalog=catalog,
        workspace_tools=tools,
    )

    assigned_refs = [
        ref
        for name, args in tool_calls
        if name == "upsert_triage_source_set"
        for ref in args["source_refs"]
    ]
    assert completion.ready is True
    assert assigned_refs == refs
    assert len(assigned_refs) == len(set(assigned_refs)) == 36
    assert [name for name, _args in tool_calls].count("list_source_capsules") == 0
    assert tool_calls[-1][0] == "finalize_triage_workspace_plan"


@pytest.mark.asyncio
async def test_capsule_cache_is_ref_neutral_and_preview_metadata_is_honest() -> None:
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    profile = SourceProfile(
        project_id=str(uuid.uuid4()),
        source_id=str(source_id),
        source_version_id=str(version_id),
        outline_hints=["source window 1", "Meaningful heading " + "x" * 220, "Meaningful heading " + "x" * 220],
        readability_warnings=["Concrete warning " + "y" * 350],
    )
    profile_record = SimpleNamespace(
        id=profile_id,
        source_id=source_id,
        source_version_id=version_id,
        is_current=True,
        payload=profile.model_dump(mode="json"),
    )
    version = SourceVersion(
        id=version_id,
        source_id=source_id,
        project_id=uuid.UUID(profile.project_id),
        kind="extracted_text",
        storage_path="source.txt",
        content_hash="a" * 64,
        status=V2RecordStatus.READY,
        metadata_={},
    )
    marker = f"[source_id={source_id} page_marker=1 paragraph=1]"
    raw_window = f"{marker} First claim for {source_id}. " + "z" * 800
    card = SourceCard(
        source_id=str(source_id),
        source_version_ids=[str(version_id)],
        representation_ids=[str(version_id)],
        source_profile_id=str(profile_id),
        title=f"Title {source_id}",
        domain=f"{version_id}.example.test",
        source_type="web",
        summary=f"Summary {profile_id}",
        gaps_or_cautions=["Concrete warning " + "y" * 350],
    )

    class _Db:
        flush_count = 0

        async def get(self, _model, item_id):
            return profile_record if item_id == profile_id else None

        async def flush(self):
            self.flush_count += 1

    class _Toolset:
        db = _Db()
        window_calls = 0

        async def _resolve_source_version(self, **_kwargs):
            return version

        async def _source_windows(self, _version, max_windows: int = 3):
            self.window_calls += 1
            return [
                {
                    "locator_text": f"Section for {version_id} " + "L" * 120,
                    "locator_type": "section",
                    "snippet_text": raw_window,
                }
            ]

    toolset = _Toolset()
    first = await build_capsule_for_card(toolset, card=card, source_ref="S01")
    second = await build_capsule_for_card(toolset, card=card, source_ref="S99")
    first_encoded = json.dumps(first, ensure_ascii=False)
    second_encoded = json.dumps(second, ensure_ascii=False)
    cache_encoded = json.dumps(version.metadata_["v2_triage_capsule"]["body"], ensure_ascii=False)

    assert raw_window.startswith(marker)
    assert toolset.window_calls == 1
    assert "source window" not in first_encoded.casefold()
    assert str(source_id) not in first_encoded
    assert str(version_id) not in first_encoded
    assert str(profile_id) not in first_encoded
    assert "source_id=" not in first_encoded
    assert "\ue000" not in first_encoded
    assert str(source_id) not in cache_encoded
    assert str(version_id) not in cache_encoded
    assert "S01" not in cache_encoded
    assert "S99" not in cache_encoded
    assert "S01" not in second_encoded
    assert "S99" in second_encoded
    assert first["outline_total_count"] == 1
    assert first["outline_item_chars"] == [len("Meaningful heading " + "x" * 220)]
    assert first["readability_warning_total_count"] == 1
    assert first["readability_warning_item_chars"] == [len("Concrete warning " + "y" * 350)]
    assert first["windows"][0]["preview_text_truncated"] is True
    assert first["windows"][0]["preview_text_chars"] == len(
        "First claim for S01. " + "z" * 800
    )
    assert first["windows"][0]["locator_truncated"] is True

    workspace = {
        "source_order": ["S01"],
        "source_sets": [{"set_id": "set-1", "source_refs": ["S01"]}],
        "source_capsules": {"S01": first},
    }
    prompt_capsule = source_set_prompt_payload(workspace, "set-1")["capsule_previews"][0]
    assert prompt_capsule["outline"][0]["text"] == (
        "Meaningful heading " + "x" * 220
    )[:100]
    assert prompt_capsule["outline"][0]["text_omitted_chars"] == len(
        "Meaningful heading " + "x" * 220
    ) - 100
    assert prompt_capsule["readability_warnings"][0]["text_omitted_chars"] == len(
        "Concrete warning " + "y" * 350
    ) - 300


@pytest.mark.asyncio
async def test_evidence_preview_sanitizes_known_ids_and_reports_omitted_text() -> None:
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    marker = f"[source_id={source_id} page_marker=3 paragraph=8]"
    raw_snippet = f"{marker} Claim for {source_id}. " + "evidence " * 100
    inventory = SourceMap(
        source_cards=[
            SourceCard(
                source_id=str(source_id),
                source_version_ids=[str(version_id)],
                representation_ids=[str(version_id)],
                source_profile_id=str(profile_id),
                title=f"Title {source_id} {profile_id}",
                domain=f"{version_id}.example.test",
                source_type="web",
                summary="Neutral source summary.",
            )
        ]
    )
    context = build_source_ref_context(inventory)

    class _Payloads(EvidencePayloadMixin):
        async def _source_ref_context(self):
            return context

    item = SimpleNamespace(
        id=uuid.uuid4(),
        source_id=source_id,
        source_version_id=version_id,
        snippet_text=raw_snippet,
        locator_text=f"Section {version_id}",
        locator_type="section",
        score=0.9,
        metadata_={},
        query_text="query",
        purpose="research",
        retrieval_channel="local_chunk_search",
    )

    payload = await _Payloads()._evidence_preview_payload(item, max_short_snippet_chars=80)  # type: ignore[arg-type]
    encoded = json.dumps(payload, ensure_ascii=False)

    assert item.snippet_text == raw_snippet
    assert str(source_id) not in encoded
    assert str(version_id) not in encoded
    assert str(profile_id) not in encoded
    assert "source_id=" not in encoded
    assert payload["support_status"] == "preview_only"
    assert payload["exact_read_next_action"] == "read_evidence"
    assert payload["short_snippet_chars"] > len(payload["short_snippet"])
    assert payload["short_snippet_truncated"] is True
    assert payload["short_snippet_omitted_chars"] == (
        payload["short_snippet_chars"] - len(payload["short_snippet"])
    )
