"""Any report format is context; only retained originals can supply E evidence."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace as NS
from uuid import uuid4, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.research.materials import targeted_resolution
from podgen.research.materials.admissions import admit_report
from podgen.research.materials.report_navigation import KEY, VERSION, reported_links, source_links
from podgen.research.materials.targeted_reparse import cached_result, save_result
from podgen.runtime.models.research_catalog import citation_page
from podgen.runtime.models.research_history import ResearchHistory
from podgen.runtime.models.research_tools import ResearchTools
from podgen.sources.material_requests import SourceAdmission
from tests.test_research_runtime import research_setup


class Store:
    def __init__(self):
        self.run_id = uuid4()
        self.records = {}

    def ref(self, key):
        return RuntimeRef(record_kind="tool_execution", record_id=uuid5(self.run_id, key))

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, value):
        if key in self.records:
            assert self.records[key] == value
        self.records[key] = deepcopy(value)


def child(text):
    store = Store()
    store.text = text
    store.records["result"] = dict(
        status="completed", report_text_blob=dict(sha256=sha256(text.encode()).hexdigest()), material={}
    )
    store.bytes = lambda *a: text.encode()
    return store


def materials():
    original = NS(
        source_id="S11",
        title="Original source",
        reported_url="https://example.com/reported",
        resolved_url="https://example.com/original",
        status="available",
        document="doc",
    )
    return NS(shared_ref=Store().ref("shared"), packet=NS(sources=[original]), runtime_admissions=[])


def load_fake(store, row):
    source = row["source"]
    return SourceAdmission(
        source["source_id"],
        source["title"],
        store.text,
        row["sha256"],
        RuntimeRef.model_validate(source["report_ref"]),
        source,
    )


@pytest.mark.parametrize(
    "text",
    [
        "# Answer\n\nAn important comparison. [cite: 11]\n\n**Sources:**\n11. [Original](https://example.com/original)\n",
        "An important comparison with https://example.com/original in plain text.",
        '{"answer":"first", "answer":"second"}',
        '```json\n{"answer":"a report, not validated evidence"}\n```',
        "<p>Another textual format.</p>",
        "The date is unknown because the original is missing.",
        '{"answer":"The date is unknown.","evidence":[],"limitations":"The original is missing."}',
    ],
)
def test_any_text_delivered_exactly_without_source_acquisition(monkeypatch, text):
    saved = child(text)
    store = Store()
    store.text = text
    original = materials()
    monkeypatch.setattr("podgen.research.materials.admissions.load_admission", load_fake)
    resolver = NS(materials=original, report_delivery=VERSION)
    resolver.restore = lambda **kw: tuple(
        load_fake(store, value)
        for key, value in store.records.items()
        if key.startswith("material-admission:")
    )
    resolver.document = lambda *a: pytest.fail("report delivery fetched a document")
    frozen = deepcopy(saved.records)
    args = dict(question="An important comparison")
    first = targeted_resolution.resolve(resolver, store, "request", saved, args, lambda: None)
    assert first.result["report_id"] == "X1" and first.result[KEY] == VERSION
    assert first.result["relationship"] == "research_context_not_primary_source_evidence"
    assert "excerpts" not in first.result and "answer" not in first.result
    from podgen.research.materials.report_delivery import terminal_result

    terminal = terminal_result(first.result)
    assert terminal["report_id"] == "X1"
    assert terminal["status"] == ("resolved" if "https://example.com/original" in text else "unresolved")
    excerpt = first.result["report_excerpt"]
    assert text[excerpt["start"] : excerpt["end"]] == excerpt["text"]
    again = targeted_resolution.resolve(resolver, store, "request", saved, args, lambda: None)
    assert again == first and len(store.records) == 1 and saved.records == frozen


def test_reading_pages_and_source_navigation_only_for_new_admission(monkeypatch):
    journal, _, _, corpus = research_setup(monkeypatch)
    text = "Context. [cite: 11]\n\n" + "Other paragraph. " * 1000
    corpus.supplemental_reports = dict(X2=NS(title="Exact question", text=text))
    corpus._materials = materials()
    catalog = dict(
        kind="research_report",
        **{KEY: VERSION},
        reported_source_map=[
            dict(reported_url="https://example.com/original", report_numbers=[11], title="Original")
        ],
    )
    corpus.admission_catalog = dict(X2=catalog)
    proxy = ResearchTools(journal, journal.root, corpus, ResearchHistory(journal, journal.root), lambda: None)
    args = dict(result_id="X2", start=0, end=None)
    page = proxy.execute("read_research_result", args, read_id="E9")
    assert page["material_passage"]["read_id"] == "R9"
    assert page["material_passage"]["text"] == text[:9600]
    assert page["next_start"] == 9600
    assert page["sources"][0]["open"]["source_id"] == "S11"
    second = proxy.execute("read_research_result", dict(args, start=9600), read_id="E10")
    assert page["material_passage"]["text"] + second["material_passage"]["text"] == text
    assert second["sources"] == []
    catalog.pop(KEY)
    legacy = proxy.execute("read_research_result", args, read_id="E9")
    assert "sources" not in legacy
    page.pop("sources")
    assert page == legacy


def test_redirects_need_exact_retained_alias_and_active_scope():
    rows = [
        dict(reported_url="https://example.com/original#paragraph", report_numbers=[7]),
        dict(reported_url="https://vertexaisearch.cloud.google.com/unknown", report_numbers=[8]),
    ]
    current = materials()
    assert source_links(rows, current)[0]["source_id"] == "S11"
    assert source_links(rows, current)[1]["source_id"] is None
    current.packet.sources = []
    assert source_links(rows, current)[0]["source_id"] is None


def test_pagination_has_versions_and_does_not_acquire_or_read_sources():
    current = materials()
    mapping = [dict(reported_url=f"https://example.com/{i}", report_numbers=[i]) for i in range(11, 49)]
    corpus = NS(
        _materials=current,
        admission_catalog={"X2": dict(kind="research_report", **{KEY: VERSION}, reported_source_map=mapping)},
    )
    pages = []
    offset = 0
    version = None
    while True:
        page = citation_page(corpus, dict(result_id="X2", offset=offset, version=version))
        pages.extend(page["sources"])
        version = page["catalog_version"]
        offset = page["next_offset"]
        if offset is None:
            break
    assert len(pages) == 38 and pages[0]["report_numbers"] == [11]
    current.runtime_admissions = [
        NS(
            source_id="S90",
            title="Now retained",
            catalog=dict(reported_url="https://example.com/11", status="available"),
        )
    ]
    assert (
        citation_page(corpus, dict(result_id="X2", offset=6, version=version))["error"]
        == "source_catalog_changed"
    )


def test_local_derived_result_survives_interruption_and_preserves_old_result():
    store = Store()
    args = dict(research_contract="targeted-script-context/1")
    old = dict(
        status="unresolved",
        error="targeted_research_invalid_answer",
        validation_contract="targeted-research-validation/2",
    )
    store.put("request:result", old)
    key, saved = cached_result(store, "request", "request_research", args, delivery=VERSION)
    assert saved is None and ":delivery:" in key
    result = dict(status="resolved", **{KEY: VERSION}, report_id="X2")
    save_result(store, "request", key, result)
    assert cached_result(store, "request", "request_research", args, delivery=VERSION) == (key, result)
    assert store.get("request:result") == old
    assert store.get(key)["original_result_ref"] == store.ref("request:result").model_dump(mode="json")
    store.records[key]["original_result_ref"] = {}
    with pytest.raises(ContractError, match="provenance"):
        cached_result(store, "request", "request_research", args, delivery=VERSION)


def test_changed_delivery_uses_new_admission_without_replacing_old(monkeypatch):
    saved = child("Preserved report")
    store = Store()
    store.text = saved.text
    monkeypatch.setattr("podgen.research.materials.admissions.load_admission", load_fake)
    original = admit_report(store, materials(), saved, "Original question", [])
    new = admit_report(store, materials(), saved, "Original question", [], delivery_contract=VERSION)
    assert original.source_id == "X1" and new.source_id == "X2"
    assert KEY not in original.catalog and new.catalog[KEY] == VERSION
    assert admit_report(store, materials(), saved, "Original question", [], delivery_contract=VERSION) == new


def test_ambiguous_numbers_do_not_assert_source_identity():
    text = "**Sources:**\n11. [One](https://example.com/one)\n11. [Two](https://example.com/two)"
    rows = reported_links(text, {})
    assert len(rows) == 2 and all(row["report_numbers"] == [] for row in rows)


def test_structured_answer_preserves_literal_locations_and_rejects_fabrication(monkeypatch):
    import json

    from podgen.sources.material_requests import MaterialResolution
    from podgen.writing.source_tools import SourceCorpus

    original_text = "A literal original statement. " * 20
    text = json.dumps(
        dict(
            answer="Synthesis.",
            limitations="",
            evidence=[
                dict(
                    url="https://example.com/original",
                    quote="A literal original statement.",
                    relation="Exact words.",
                ),
                dict(
                    url="https://example.com/original", quote="Fabricated words.", relation="Not established."
                ),
            ],
        )
    )
    saved = child(text)
    store = Store()
    store.text = text
    current = materials()
    current.documents = []
    current.packet.documents = []
    source = SourceAdmission(
        "S11",
        "Original",
        original_text,
        sha256(original_text.encode()).hexdigest(),
        store.ref("source"),
        dict(reported_url="https://example.com/original", status="available"),
    )
    monkeypatch.setattr("podgen.research.materials.admissions.load_admission", load_fake)
    resolver = NS(materials=current, report_delivery=VERSION)
    resolver.restore = lambda **kw: (
        source,
        *(
            load_fake(store, value)
            for key, value in store.records.items()
            if key.startswith("material-admission:")
        ),
    )
    requested = []

    def document(*args):
        requested.append(args[1])
        return MaterialResolution(dict(source_id="S11", status="resolved", availability="available"))

    resolver.document = document
    result = targeted_resolution.resolve(
        resolver, store, "request", saved, dict(question="Exact words"), lambda: None
    )
    assert requested == ["https://example.com/original"]
    assert result.result["excerpts"] == [
        dict(source_id="S11", quote="A literal original statement.", start=0, end=29, relation="Exact words.")
    ]
    assert result.result["rejected_evidence"] == [
        dict(evidence_index=1, error="targeted_research_quote_not_in_source")
    ]
    corpus = SourceCorpus(
        NS(packet=NS(documents=[], dossier=NS(knowledge_cards=[])), documents=[], runtime_admissions=[])
    )
    corpus.admit(source)
    locator = result.result["excerpts"][0]
    evidence = corpus.execute(
        "read_source",
        {key: locator[key] for key in ("source_id", "start", "end")},
        read_id="E3",
        max_read_chars=9600,
    )
    assert evidence["passage"]["read_id"] == "E3" and evidence["passage"]["text"] == locator["quote"]


def test_entry_point_uses_pinned_contract_without_rereading_manifest(monkeypatch):
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.service import WritingEvaluation

    journal, _, _, _ = research_setup(monkeypatch)
    snapshot = deepcopy(journal.snapshot)
    assert snapshot["source_tool_contract"][KEY] == VERSION
    monkeypatch.setattr(EvaluationJournal, "run", lambda *a: pytest.fail("hot path reread run"))
    monkeypatch.setattr(EvaluationJournal, "manifest", lambda *a: pytest.fail("hot path reread manifest"))
    monkeypatch.setattr(
        "podgen.research.materials.resolver.ResearchMaterialResolver", lambda *a, **kw: NS(**kw)
    )
    evaluation = WritingEvaluation.__new__(WritingEvaluation)
    evaluation.repo = NS()
    evaluation.material_acquirer = None
    command = NS(
        project_id=uuid4(),
        run_id=uuid4(),
        planning_parent_run_id=None,
        editorial_parent_run_id=None,
        material_parent_run_id=None,
    )
    assert evaluation.material_resolver(command, materials(), manifest=snapshot).report_delivery == VERSION
    snapshot["source_tool_contract"].pop(KEY)
    assert evaluation.material_resolver(command, materials(), manifest=snapshot).report_delivery is None
    snapshot["source_tool_contract"][KEY] = "unknown"
    with pytest.raises(ContractError, match="unsupported_targeted_report_delivery"):
        evaluation.material_resolver(command, materials(), manifest=snapshot)


def test_concurrent_local_recovery_interprets_saved_child_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Lock

    from podgen.research.materials.report_delivery import recover_saved
    from podgen.sources.material_requests import MaterialResolution

    store = Store()
    mutex = Lock()

    @contextmanager
    def locked():
        with mutex:
            yield

    store.lock = locked
    store.put("request:result", dict(status="unresolved", error="targeted_research_invalid_answer"))
    store.put("request:handoff", dict(question="Frozen question"))
    result = MaterialResolution(dict(status="resolved", report_id="X2", **{KEY: VERSION}), ())
    calls = []

    def interpret(*args):
        calls.append(args)
        return result

    monkeypatch.setattr("podgen.research.materials.targeted_reparse.resolve_saved", interpret)
    resolver = NS(report_delivery=VERSION, store=lambda _: store, restore=lambda **kw: ())
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(
            pool.map(lambda _: recover_saved(resolver, store.run_id, "request", lambda: None), range(2))
        )
    assert answers == [result, result] and len(calls) == 1


def test_model_view_keeps_order_identity_only_in_technical_result(monkeypatch):
    journal, _, _, corpus = research_setup(monkeypatch)
    tools = ResearchTools(journal, journal.root, corpus, ResearchHistory(journal, journal.root), lambda: None)
    raw = dict(status="resolved", report_id="X2", research_order_id="a" * 64, **{KEY: VERSION})
    assert tools.model_result("request_research", raw) == dict(status="resolved", report_id="X2")
    assert raw["research_order_id"] == "a" * 64
    raw.pop(KEY)
    assert tools.model_result("request_research", raw) is raw


def test_saved_v1_terminal_decisions_reproduce_their_original_outcome():
    from podgen.research.materials.report_delivery import terminal_result
    from podgen.research.materials.report_navigation import LEGACY

    archive = {KEY: LEGACY, "status": "resolved", "report_id": "X1"}
    assert terminal_result(archive)["status"] == "resolved"
    assert terminal_result(dict(archive, limitations="Missing original"))["status"] == "unresolved"
