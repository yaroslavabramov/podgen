"""Offline navigation/replay regressions; no database, model or acquisition calls."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.research.materials.contracts import ResearchPacket, ResearchSource
from podgen.research.materials.loading import ResearchMaterials
from podgen.research.materials.sections import partition, source_mapping
from podgen.retrieval.config import profile as legacy_retrieval_profile
from podgen.runtime.evaluation.compiler import response_request
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.source_tool_contract import bind, create, from_manifest
from podgen.runtime.models.research_tools import ResearchTools
from podgen.sources.material_requests import MaterialResolution, SourceAdmission
from podgen.writing.editorial_contract import Assignment, SourceDocument
from podgen.writing.materials import AuthorDocument
from podgen.writing.source_tools import SourceCorpus, attach_task_passages, resolve_review_reads
from tests.own_plan_provider import proposal

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "evals/reports/2026-11-13-hosted-planning/runtime"
INSTRUCTION_HASHES = {
    "plan": "c279a4c862ff2f93549e3500204eb63e679037a245eb2e3a08127282a6785d5a",
    "draft": "7fb9a9a074c709c39c58f3520f1bbd4cd89b85d3e38a6e37ec3c5afb79428ffb",
    "reader-review": "7c2227a7947e77b60a6ec22584305ebfa7f24d9f36cd59d8fafdbb2df3893b77",
    "fact-review": "8df84e08233b73328c1390897f17b91dfe8c4b6a3d0cc54fe5ed127ca31b0cde",
}
SOURCE_TEXT = "An original record: 33 seconds.\r\nТочное свидетельство 😀.\r\n" * 100
SOURCE_NUMBERS = (2, 11, 20, 31, 44, 56, 58, 63)
REPORT = (
    "# Full research\n\n"
    + "RESEARCH_ONLY: полное объяснение ёжика 😀.\r\n" * 600
    + "SELECTED_SECTION_TAIL [cite: 11]\n\n## Consequences\n\n"
    + "A separate account. [cite: 2]\n\n**Sources:**\n"
    + "\n".join(f"{n}. [Record {n}](https://example.org/document/{n})" for n in SOURCE_NUMBERS)
    + "\n"
)


def _profile(stage):
    writer = stage == "draft" or stage.startswith("revise-")
    return SimpleNamespace(
        provider="anthropic" if writer else "openai",
        api_family="messages" if writer else "responses",
        adapter_version="anthropic-messages-v2" if writer else "openai-responses-v1",
        allowed_roles=["planner", "plan_reviewer", "writer", "editorial_reviewer"],
        structured_output_mode="json_schema",
        fallback_profile_refs=[],
        output_limit=24576,
        context_limit=200000,
        model_revision_or_alias=(
            "claude-opus-5-5"
            if writer
            else "gpt-6.1-sol"
            if stage in ("plan", "plan-revise")
            else "gpt-6.1-terra"
        ),
        generation_parameters=dict(
            reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
        ),
        timeout_policy=dict(rpc_timeout_seconds=60, poll_deadline_seconds=60),
    )


@pytest.fixture
def research_materials():
    assignment = json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/shared.json").read_text())[
        "assignment"
    ]
    assignment["key"] = "navigation-contract"
    report_ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    source_ref = ArtifactRef(kind="SourceRepresentation", artifact_id=uuid4(), revision_id=uuid4())
    documents = [
        SourceDocument(
            key=key, title=title, source_path=path, sha256=sha256(text.encode()).hexdigest(), purpose=purpose
        )
        for key, title, path, text, purpose in (
            ("report", "Complete research", "raw/report.md", REPORT, "dossier"),
            ("record", "Original record", "raw/record.txt", SOURCE_TEXT, "primary"),
        )
    ]
    sources = [
        ResearchSource(
            source_id=f"S{n}",
            reported_url=f"https://example.org/document/{n}",
            report_number=n,
            title=f"Record {n}",
            status="available" if n == 11 else "unavailable" if n == 44 else "not_acquired",
            document="record" if n == 11 else None,
            representation_ref=source_ref if n == 11 else None,
            coverage="Full retained text" if n == 11 else None,
            limitations=["The document could not be retrieved."] if n == 44 else [],
        )
        for n in SOURCE_NUMBERS
    ]
    mapping = source_mapping(REPORT)
    packet = ResearchPacket(
        assignment=Assignment.model_validate(assignment),
        research_assignment={"user_request": "Original commission, not an added writing obligation"},
        documents=documents,
        sections=partition(
            REPORT, sources={n: dict(url=row["url"], source_id=f"S{n}") for n, row in mapping.items()}
        ),
        sources=sources,
        report_result_ref=report_ref,
        source_result_refs=[],
    )
    return ResearchMaterials(
        packet,
        RuntimeRef(record_kind="user_input", record_id=uuid4()),
        (
            AuthorDocument(documents[0].source_path, REPORT, documents[0].sha256, report_ref),
            AuthorDocument(documents[1].source_path, SOURCE_TEXT, documents[1].sha256, source_ref),
        ),
    )


def _tools(materials):
    return ResearchTools(
        SimpleNamespace(execution=SimpleNamespace(prepare=lambda *args, **kwargs: None)),
        dict(invocation_id=uuid4(), run_id=uuid4()),
        SourceCorpus(materials),
        None,
        lambda: None,
    )


def _plan(materials):
    selected = materials.packet.sections[0].material_id
    value = proposal(dict(material_catalog=[selected], speaker_labels=materials.speaker_labels), "navigation")
    value.pop("limitations")
    for act in value["plan"]["acts"]:
        act.pop("limitations_to_voice")
    value["plan"]["ending"] = dict(
        last_moment="The recorded result.", why_end_here="The question is answered.", promises_to_close=[]
    )
    return value


@pytest.mark.parametrize("version", ["frozen-corpus-tools/5", "frozen-corpus-tools/6"])
@pytest.mark.parametrize("stage", list(INSTRUCTION_HASHES))
def test_historical_research_request_replay_keeps_full_instructions_and_wire(version, stage):
    """V6 changes the default read size, never the captured v5 instructions/payload."""
    archived = json.loads((BASELINE / f"{stage}.preview.json").read_text())["request"]
    assert sha256(archived["instructions"].encode()).hexdigest() == INSTRUCTION_HASHES[stage]
    source_tools = json.loads((BASELINE / "plan.preview.json").read_text())["request"]["tools"]
    contract = dict(
        version=version,
        definitions=deepcopy(source_tools),
        retrieval=legacy_retrieval_profile(),
        max_model_turns=12,
        max_tool_calls=24,
        final_turn="structured_without_tools",
        max_read_chars=9600,
    )
    if version.endswith("/6"):
        contract["default_read_chars"] = 9600
    frozen = deepcopy(contract)
    restored = from_manifest(dict(behavior_profile="production/1", source_tool_contract=contract))
    body = response_request(
        UUID(archived["metadata"]["podgen_run_id"]),
        stage,
        _profile(stage),
        request_payload(archived),
        archived["instructions"],
        archived["text"]["format"]["schema"],
        prompt_cache_layout=archived["metadata"]["podgen_prompt_cache_layout"],
    )
    assert bind(body, stage, _profile(stage), restored) == archived
    assert contract == frozen


def test_v7_plan_has_catalog_navigation_and_keeps_the_literary_instructions(research_materials):
    body = preview_request(uuid4(), "plan", _profile("plan"), research_materials)
    payload = request_payload(body)
    assert isinstance(payload["source_catalog"], dict)
    assert payload["source_catalog"]["tool"] == "list_sources"
    assert payload["source_catalog"]["offset"] == 0
    assert len(payload["research_contents"]) == len(research_materials.packet.sections)
    assert all("text" not in row for row in payload["research_contents"])
    for source in research_materials.packet.sources:
        assert source.reported_url not in json.dumps(payload)
    assert "RESEARCH_ONLY" not in json.dumps(payload)
    old = json.loads((BASELINE / "plan.preview.json").read_text())["request"]["instructions"]
    marker = "# Reading the supplied research materials"
    assert body["instructions"].split(marker)[0] == old.split(marker)[0]
    names = {tool["name"] for tool in body["tools"]}
    assert {"list_sources", "request_source", "request_research", "save_working_state"} <= names
    contract = create(SimpleNamespace(name="production/1"), research_materials)
    assert contract["version"] == "frozen-corpus-tools/8"
    assert not {"max_model_turns", "max_tool_calls", "final_turn"} & contract.keys()


def test_v7_writer_receives_the_full_selected_section_and_story(research_materials):
    plan = _plan(research_materials)
    body = preview_request(uuid4(), "draft", _profile("draft"), research_materials, approved_plan=plan)
    payload = request_payload(body)
    section = research_materials.packet.sections[0]
    expected = REPORT[section.start : section.end]
    assert len(expected) > 9600
    assert [row["card_id"] for row in payload["support"]] == ["M1"]
    assert payload["support"][0]["text"] == expected
    assert "SELECTED_SECTION_TAIL" in payload["support"][0]["text"]
    assert payload["support"][0]["reported_source_ids"] == ["S11"]
    assert "source_excerpts" not in payload["support"][0]
    assert payload["story"]["episode_answer"] == plan["plan"]["working_answer"]
    assert payload["story"]["ending"] == plan["plan"]["ending"]
    assert payload["source_catalog"]["tool"] == "list_sources"


@pytest.mark.parametrize("stage", ["reader-review", "fact-review"])
def test_research_navigation_respects_independent_reading_inputs(research_materials, stage):
    script = dict(utterances=[dict(speaker=research_materials.speaker_labels[0], text="Exact speech 😀.")])
    body = preview_request(uuid4(), stage, _profile(stage), research_materials, draft=script)
    payload = request_payload(body)
    assert payload["script"]["utterances"] == [dict(index=0, **script["utterances"][0])]
    if stage == "reader-review":
        assert set(payload) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
            "audit_context",
        }
        assert body["tools"] == [] and body["tool_choice"] == "none"
        archived = json.loads((BASELINE / "reader-review.preview.json").read_text())["request"]
        assert body["instructions"] == archived["instructions"]
    else:
        assert payload["evidence"] == []
        assert payload["source_bibliography"]["tool"] == "list_sources"
        assert all("text" not in row for row in payload["research_contents"])
    assert "RESEARCH_ONLY" not in json.dumps(payload)
    assert SOURCE_TEXT not in json.dumps(payload, ensure_ascii=False)


def test_catalog_pages_preserve_missing_sources_ids_urls_and_limitations(research_materials):
    tools = _tools(research_materials)
    offset, version, rows, pages = 0, None, [], []
    while offset is not None:
        args = dict(offset=offset, version=version)
        page = tools.execute("list_sources", args, read_id=f"E{len(pages) + 1}")
        assert tools.execute("list_sources", args, read_id="E99") == page
        version = version or page["catalog_version"]
        assert page["catalog_version"] == version
        pages.append(page)
        rows.extend(page["sources"])
        offset = page["next_offset"]
    assert len(pages) > 1
    assert [row["source_id"] for row in rows] == [f"S{n}" for n in SOURCE_NUMBERS]
    assert pages[-1]["total_sources"] == len(SOURCE_NUMBERS)
    by_id = {row["source_id"]: row for row in rows}
    assert by_id["S2"]["status"] == "not_acquired"
    assert by_id["S44"]["status"] == "unavailable"
    assert by_id["S44"]["limitations"] == ["The document could not be retrieved."]
    for row in rows:
        assert row["reported_url"] == f"https://example.org/document/{row['source_id'][1:]}"
        assert "text" not in row and "read_id" not in row


def test_catalog_version_rejects_mixing_pages_after_an_admission(research_materials):
    tools = _tools(research_materials)
    first = tools.execute("list_sources", dict(offset=0, version=None), read_id="E1")
    admission = SourceAdmission(
        "S99",
        "New record",
        "New source body.",
        sha256(b"New source body.").hexdigest(),
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        dict(status="available", reported_url="https://example.org/new-record"),
    )
    research_materials.runtime_admissions.append(admission)
    tools.corpus.admit(admission)
    stale = tools.execute(
        "list_sources", dict(offset=first["next_offset"], version=first["catalog_version"]), read_id="E2"
    )
    assert stale["error"] == "source_catalog_changed" and stale["restart_offset"] == 0
    assert stale["catalog_version"] != first["catalog_version"]


def test_a_missing_catalog_source_can_be_requested_by_its_exact_id(research_materials):
    tools = _tools(research_materials)
    calls = []

    class PendingResolver:
        def resolve(self, name, arguments, **kwargs):
            calls.append((name, arguments, kwargs["operation_id"]))
            return MaterialResolution(dict(status="pending", handoff_ref={"record_id": "saved-handoff"}))

        def restore(self, **kwargs):
            return ()

    tools.corpus.material_resolver = PendingResolver()
    row = tools.execute("list_sources", dict(offset=0, version=None), read_id="E1")["sources"][0]
    result = tools.execute(
        "request_source",
        dict(source_id=row["source_id"], url=None, reason="Verify this cited record."),
        read_id="E2",
    )
    assert result["status"] == "pending"
    assert len(calls) == 1
    assert calls[0][:2] == (
        "request_source",
        dict(source_id="S2", url=None, reason="Verify this cited record."),
    )


def test_supplemental_research_and_report_reads_cannot_become_source_evidence(research_materials):
    synthesis = "Research synthesis is not testimony. 😀\r\n" * 350
    research_materials.runtime_admissions.append(
        SourceAdmission(
            "X1",
            "Targeted answer",
            synthesis,
            sha256(synthesis.encode()).hexdigest(),
            RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
            dict(kind="research_report"),
        )
    )
    tools = _tools(research_materials)
    report_page = tools.execute(
        "read_material",
        dict(material_id="M1", start=0, end=150, context=False),
        read_id="E40",
    )
    assert report_page["material_passage"]["read_id"] == "R40"
    assert report_page["material_passage"]["text"] == REPORT[:150]
    assert "passage" not in report_page
    parts, cursor, count = [], 0, 0
    while cursor is not None:
        count += 1
        result = tools.execute(
            "read_research_result", dict(result_id="X1", start=cursor, end=None), read_id=f"E{count}"
        )
        passage = result["material_passage"]
        assert passage["read_id"] == f"R{count}" and "passage" not in result
        parts.append(passage["text"])
        cursor = result["next_start"]
    assert count > 1 and "".join(parts) == synthesis
    assert "X1" not in tools.corpus.documents
    assert (
        tools.corpus.execute("read_source", dict(source_id="X1", start=0, end=None), read_id="E8")["error"]
        == "unknown_source_id"
    )
    opened = tools.execute("read_source", dict(source_id="S11", start=0, end=150), read_id="E9")["passage"]
    assert opened["read_id"] == "E9" and opened["text"] == SOURCE_TEXT[:150]
    review = dict(findings=[dict(source_read_ids=["R1"])])
    with pytest.raises(ContractError, match="unread_or_duplicate_source_reference"):
        resolve_review_reads(review, [opened], research_materials)
    review["findings"][0]["source_read_ids"] = ["E9"]
    resolved = resolve_review_reads(review, [opened], research_materials)
    task = deepcopy(resolved["findings"][0])
    payload = dict(revision_tasks=[task])
    attach_task_passages(payload, research_materials)
    assert task["source_read_refs"] == ["P1"]
    assert payload["review_source_passages"][0]["text"] == SOURCE_TEXT[:150]
    assert "R1" not in json.dumps(payload)


@pytest.mark.parametrize(
    "reported_url, linked",
    [
        ("https://example.org/document/2", True),
        ("https://example.org/document/2#evidence", True),
        ("https://EXAMPLE.ORG:443/document/2#evidence", True),
        ("https://example.org/Document/2", False),
        ("https://example.org/document/2?version=A", False),
        ("https://example.org:8443/document/2", False),
        ("http://example.org/document/2", False),
    ],
)
def test_admitted_url_alias_is_searchable_within_its_original_material(
    research_materials, reported_url, linked
):
    source = next(row for row in research_materials.packet.sources if row.source_id == "S2")
    source = source.model_copy(update={"reported_url": reported_url})
    research_materials = replace(
        research_materials,
        packet=research_materials.packet.model_copy(
            update={
                "sources": [
                    source if row.source_id == "S2" else row for row in research_materials.packet.sources
                ],
            }
        ),
    )
    corpus = SourceCorpus(research_materials)
    scope = next(
        alias
        for alias, section in corpus.research_sections.items()
        if any(item.source_id == "S2" for item in section.occurrences)
    )
    original_catalog = [row.model_dump(mode="json") for row in research_materials.packet.sources]
    text = "Transportaliasneedle is retained exactly."
    catalog = dict(reported_url="https://example.org/document/2", source_id="S64")
    admission = SourceAdmission(
        "S64",
        "Acquired source",
        text,
        sha256(text.encode()).hexdigest(),
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        catalog,
    )
    assert "S64" not in corpus.documents
    corpus.admit(admission)
    corpus.admit(admission)  # Durable admission replay must not duplicate scope entries.
    matches = corpus.execute(
        "search_sources", dict(query="Transportaliasneedle", within=scope, offset=0), read_id="unused"
    )["hits"]
    assert [row["source_id"] for row in matches] == (["S64"] if linked else [])
    assert sum(key == "S64" for key, _, _ in corpus.cards[scope]) == int(linked)
    assert (
        corpus.execute("read_source", dict(source_id="S64", start=0, end=None), read_id="E1")["passage"][
            "text"
        ]
        == text
    )
    assert [row.model_dump(mode="json") for row in research_materials.packet.sources] == original_catalog
    assert source.source_id == "S2" and source.reported_url == reported_url
    assert corpus.admission_catalog["S64"] == catalog


@pytest.mark.parametrize("stage", ["plan", "draft"])
def test_v8_preserves_complete_non_fact_request(research_materials, monkeypatch, stage):
    from podgen.runtime.evaluation import source_tool_contract

    run = uuid4()
    inputs = dict(approved_plan=_plan(research_materials)) if stage == "draft" else {}
    current = create(SimpleNamespace(name="production/1"), research_materials)
    original = deepcopy(current)
    original["version"] = "frozen-corpus-tools/7"
    original.pop("fact_request_research")
    original.pop("web_search_definition")
    after = preview_request(run, stage, _profile(stage), research_materials, **inputs)
    monkeypatch.setattr(source_tool_contract, "create", lambda *args: original)
    before = preview_request(run, stage, _profile(stage), research_materials, **inputs)
    assert after == before
    tool = next(row for row in after["tools"] if row["name"] == "request_research")
    assert set(tool["parameters"]["properties"]) == {"question", "reason"}
