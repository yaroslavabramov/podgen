"""Exact optional H reads and inline fallback under the delta input contract."""

from copy import deepcopy

import pytest


@pytest.mark.parametrize(
    "mode", ["locator", "affected", "several_selectors", "context_only", "renumbered", "long"]
)
def test_retained_h_compaction_has_exact_optional_read_and_preserves_backend(monkeypatch, mode):
    from podgen.runtime.evaluation.reading_audit.recheck_evidence import compact
    from tests.test_reading_audit_evidence import catalog

    materials, corpus, root, _, _, journal, index = catalog(monkeypatch)
    from podgen.runtime.evaluation.reading_audit.evidence import current_catalog

    value = deepcopy(current_catalog(journal, root, materials, index, corpus)["E1"])
    if mode == "several_selectors":
        value["source_selectors"].append(deepcopy(value["source_selectors"][0]))
    elif mode == "context_only":
        value["source_selectors"][0]["key"] = "context-doc"
        index["context-doc"] = dict(
            index[value["source_selectors"][0]["key"]]
            if value["source_selectors"][0]["key"] in index
            else next(iter(index.values()))
        )
    elif mode == "renumbered":
        # The source alias stored in an old content/locator is not authority for this scope.
        value["content"]["source_id"] = "S999"
    elif mode == "long":
        value["source_selectors"][0].update(start=10, end=20010)
    frozen = deepcopy(value)
    context = dict(
        previous_decisions=[
            dict(
                requires_update=mode == "affected",
                verdict="supported",
                source_refs=[dict(from_review="previous", evidence_id="H1")],
            )
        ],
        previous_evidence=[
            dict(evidence_id="H1", original_id="E1", kind="source_read", content=deepcopy(value["content"]))
        ],
    )
    compact(context, dict(H1=value), materials, index)
    result = context["previous_evidence"][0]
    if mode in ("affected", "several_selectors", "context_only"):
        assert result["content"] == value["content"] and "read_source" not in result
    else:
        assert "content" not in result and result["source_id"] == "S1"
        assert result["read_source"]["start"] == value["source_selectors"][0]["start"]
        if mode == "long":
            assert result["end"] == 20010 and result["read_source"]["end"] == 20010
        else:
            action = dict(result["read_source"])
            action.pop("tool")
            read = corpus.execute(
                "read_source", action, read_id="E99", max_read_chars=9600, default_read_chars=9600
            )
            assert read["passage"]["text"] == value["content"]["text"]
    assert value == frozen


def test_absent_read_source_keeps_full_evidence_even_when_selector_is_unique(monkeypatch):
    from podgen.runtime.evaluation.reading_audit.evidence import current_catalog
    from podgen.runtime.evaluation.reading_audit.recheck_evidence import compact
    from tests.test_reading_audit_evidence import catalog

    materials, corpus, root, _, _, journal, index = catalog(monkeypatch)
    value = current_catalog(journal, root, materials, index, corpus)["E1"]
    context = dict(
        previous_decisions=[],
        previous_evidence=[
            dict(evidence_id="H1", original_id="E1", kind="source_read", content=value["content"])
        ],
    )
    frozen = deepcopy(context)
    compact(context, dict(H1=value), materials, index, read_source_available=False)
    assert context == frozen


def test_long_optional_read_uses_normal_pagination_to_the_original_range_end():
    from uuid import uuid4

    from podgen.domain.contracts.common import RuntimeRef
    from podgen.runtime.evaluation.reading_audit.evidence import snapshots
    from podgen.runtime.evaluation.reading_audit.recheck_evidence import compact
    from podgen.writing.source_tools import SourceCorpus
    from tests.test_frozen_source_tools import materials

    raw = "αβγδε " * 5000
    source = materials(raw)
    source.documents[0].ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    index = {row["key"]: row for row in snapshots(source)}
    content = dict(source="Book", source_id="S1", start=10, end=22010, text=raw[10:22010])
    selector = dict(key="k", sha256=index["k"]["sha256"], ref=index["k"]["ref"], start=10, end=22010)
    context = dict(
        previous_decisions=[],
        previous_evidence=[dict(evidence_id="H1", original_id="E1", kind="source_read", content=content)],
    )
    compact(context, dict(H1=dict(source_selectors=[selector])), source, index)
    args = dict(context["previous_evidence"][0]["read_source"])
    args.pop("tool")
    corpus = SourceCorpus(source)
    parts = []
    while True:
        page = corpus.execute("read_source", args, read_id="E9", max_read_chars=2400, default_read_chars=2400)
        parts.append(page["passage"]["text"])
        if page["next_start"] is None:
            break
        args["start"] = page["next_start"]
    assert "".join(parts) == content["text"]


def test_unavailable_source_in_current_tool_scope_stays_inline(monkeypatch):
    from types import SimpleNamespace as NS

    from podgen.runtime.evaluation.reading_audit.evidence import current_catalog
    from podgen.runtime.evaluation.reading_audit.recheck_evidence import compact
    from tests.test_reading_audit_evidence import catalog

    materials, corpus, root, _, _, journal, index = catalog(monkeypatch)
    value = current_catalog(journal, root, materials, index, corpus)["E1"]
    source_key = value["source_selectors"][0]["key"]
    current = NS(
        packet=NS(
            contract="research-editorial-input/1",
            sources=[NS(source_id="S1", document=source_key, status="unavailable")],
        ),
        runtime_admissions=[],
    )
    context = dict(
        previous_decisions=[],
        previous_evidence=[
            dict(evidence_id="H1", original_id="E1", kind="source_read", content=value["content"])
        ],
    )
    compact(context, dict(H1=value), current, index)
    assert context["previous_evidence"][0]["content"] == value["content"]


def test_new_contract_does_not_turn_ambiguous_packet_h_into_a_source_read(monkeypatch):
    from podgen.domain.errors import ContractError
    from podgen.runtime.evaluation.reading_audit import evidence
    from podgen.runtime.evaluation.reading_audit.lifecycle import _locator
    from tests.test_reading_audit_evidence import catalog

    materials, corpus, root, _, _, journal, index = catalog(monkeypatch)
    value = deepcopy(evidence.current_catalog(journal, root, materials, index, corpus)["B1"])
    value["source_selectors"].append(deepcopy(value["source_selectors"][0]))
    with pytest.raises(ContractError, match="audit_finding_read_ambiguous"):
        _locator(value, materials, index, corpus, require_unique=True)
    assert _locator(value, materials, index, corpus).startswith("S1:")  # Historical replay unchanged.
