"""Report-off boundaries and exact pinned wire changes; no external providers."""

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.errors import Conflict, ContractError
from podgen.retrieval.config import streaming_profile
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects
from podgen.runtime.evaluation.fact_source_experiment.service import ApprovalBoundGateway
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.source_tool_contract import create, from_manifest
from podgen.runtime.models.research_tools import ResearchTools
from podgen.writing.research_materials import fact_source_mode as mode
from podgen.writing.research_materials.runtime_tools import definitions as runtime_definitions
from podgen.writing.research_materials.runtime_tools import source_definitions
from podgen.writing.research_materials.tools import definitions as research_definitions
from podgen.writing.source_tools import SourceCorpus, definitions
from tests.test_research_navigation_contract import SOURCE_TEXT, _profile
from tests.test_research_navigation_contract import research_materials as materials_fixture


@pytest.fixture
def research_materials():
    return materials_fixture.__wrapped__()


def test_exact_wire_changes_keep_the_existing_fact_judgment(research_materials):
    script = dict(utterances=[dict(speaker=research_materials.speaker_labels[0], text="Exact speech.")])
    before = preview_request(
        uuid4(), "fact-review", _profile("fact-review"), research_materials, draft=script
    )
    frozen = deepcopy(before)
    after = mode.request(before, mode.VERSION)
    assert before == frozen
    assert mode.request(before, None) is before
    assert mode.request(after, mode.VERSION) == after
    before_payload, after_payload = request_payload(before), request_payload(after)
    assert set(before_payload) - set(after_payload) == {"research_contents", "research_results"}
    assert after_payload == {k: v for k, v in before_payload.items() if k not in mode.HIDDEN_FIELDS}
    assert {t["name"] for t in after["tools"]} == mode.ALLOWED_TOOLS
    changed = {"instructions", "tools", "input", "text"}
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed
    }
    old_schema = deepcopy(before["text"]["format"]["schema"])
    new_schema = deepcopy(after["text"]["format"]["schema"])
    old_fields = old_schema["$defs"]["SourcedFactFinding"]["properties"]
    new_fields = new_schema["$defs"]["SourcedFactFinding"]["properties"]
    assert "maxItems" not in new_fields["support_labels"]
    for fields in (old_fields, new_fields):
        fields["support_labels"].pop("description")
        fields["support_labels"]["items"].pop("enum", None)
        fields["source_read_ids"].pop("description")
    assert old_schema == new_schema
    assert "read_material" not in after["instructions"]
    assert "request_research" not in after["instructions"]


def test_same_sources_and_catalog_without_report_text(research_materials):
    original = SourceCorpus(research_materials)
    corpus = SourceCorpus(research_materials, fact_input_mode=mode.VERSION)
    assert corpus.documents == original.documents
    assert corpus.research_sources == original.research_sources
    assert not any(
        (corpus.cards, corpus.research_texts, corpus.research_sections, corpus.supplemental_reports)
    )
    result = corpus.execute("read_source", dict(source_id="S11", start=0, end=150), read_id="E1")
    assert result["passage"]["text"] == SOURCE_TEXT[:150]


@pytest.mark.parametrize(
    "name",
    [
        "read_material",
        "read_research_result",
        "list_research_results",
        "list_research_sources",
        "select_research_result",
        "request_research",
    ],
)
def test_hidden_tools_rejected_at_both_execution_boundaries(research_materials, name):
    corpus = SourceCorpus(research_materials, fact_input_mode=mode.VERSION)
    corpus.material_resolver = SimpleNamespace(resolve=lambda *a, **k: pytest.fail("resolver invoked"))
    executor = ResearchTools(None, dict(invocation_id=uuid4(), run_id=uuid4()), corpus, None, lambda: None)
    assert executor.execute(name, {}, read_id="E1") == dict(error="unknown_source_tool")
    assert corpus.execute(name, {}, read_id="E1") == dict(error="unknown_source_tool")


@pytest.mark.parametrize("query", ["", "original"])
def test_report_scope_never_falls_back_to_all_documents(research_materials, query):
    corpus = SourceCorpus(research_materials, fact_input_mode=mode.VERSION)
    assert corpus.execute("search_sources", dict(query=query, within="M1", offset=0), read_id="E1") == dict(
        error="unknown_source_or_material"
    )


def test_raw_history_contract_is_preserved_and_mode_is_explicit(research_materials):
    contract = create(SimpleNamespace(name="production/1"), research_materials)
    contract.pop("history_view")
    contract["definitions"] = (
        source_definitions(research_definitions(definitions(retrieval=True))) + runtime_definitions()
    )
    old = deepcopy(contract)
    assert from_manifest(dict(behavior_profile="production/1", source_tool_contract=contract)) == old
    contract[mode.KEY] = mode.VERSION
    contract["definitions"] = mode.tools(contract["definitions"])
    manifest = dict(
        behavior_profile="production/1",
        source_tool_contract=contract,
        stages=["fact-review"],
        **{mode.KEY: mode.VERSION},
    )
    assert from_manifest(manifest) == contract
    assert "history_view" not in contract
    assert (
        "JSON" in next(t for t in contract["definitions"] if t["name"] == "read_work_history")["description"]
    )
    for change in ({"stages": ["draft", "fact-review"]}, {mode.KEY: None}):
        with pytest.raises(ContractError):
            from_manifest(dict(manifest, **change))
    assert contract["retrieval"] == streaming_profile()


def test_unapproved_request_never_constructs_provider():
    gateway = ApprovalBoundGateway(
        SimpleNamespace(request=lambda _: {"request": "different"}),
        lambda _: pytest.fail("provider constructed"),
        approved_request_hash="wrong",
    )
    with pytest.raises(Conflict, match="fact_experiment_request_not_approved"):
        gateway.invoke({})


def test_object_measurements_count_staging_publication_and_reads_once(tmp_path):
    objects = MeasuredObjects(tmp_path)
    project = uuid4()
    ref = objects.put(project, BytesIO(b"abcdef"), "text/plain")
    assert objects.counts["stage_calls"] == objects.counts["publish_calls"] == 1
    assert objects.counts["staged_bytes"] == objects.counts["published_bytes"] == 6
    # Publication verifies the complete stored object once.
    assert objects.counts["read_bytes"] == 6
    assert objects.read(project, ref) == b"abcdef"
    assert objects.counts["read_bytes"] == 12
    assert b"".join(objects.iter_read(project, ref, block_size=2)) == b"abcdef"
    assert objects.counts["read_bytes"] == 18


@pytest.mark.parametrize("query", ["", "original"])
def test_streaming_report_scope_is_rejected_before_search(research_materials, query, monkeypatch):
    from tests.source_search_support import TinyModels
    from tests.test_streaming_source_search import CountingIndex

    monkeypatch.setattr("podgen.retrieval.streaming.models", lambda _: TinyModels())
    index = CountingIndex()
    index.access = SimpleNamespace(admission_references=[])
    index.check_access = lambda: None
    corpus = SourceCorpus(
        research_materials, fact_input_mode=mode.VERSION, retrieval=streaming_profile(), index=index
    )
    result = corpus.execute(
        "search_sources", dict(query=query, within="M1", offset=0, result_version=None), read_id="E1"
    )
    assert result == dict(error="unknown_source_or_material")


def test_later_report_admission_cannot_expose_report(research_materials):
    from hashlib import sha256

    from podgen.domain.contracts.common import RuntimeRef
    from podgen.sources.material_requests import SourceAdmission

    corpus = SourceCorpus(research_materials, fact_input_mode=mode.VERSION)
    text = "Hidden supplemental synthesis."
    corpus.admit(
        SourceAdmission(
            "X1",
            "Hidden report",
            text,
            sha256(text.encode()).hexdigest(),
            RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
            dict(kind="research_report"),
        )
    )
    assert corpus.supplemental_reports == {} and corpus.admission_catalog == {}
    assert "X1" not in corpus.documents
