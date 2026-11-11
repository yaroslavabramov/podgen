"""Lossless shared packet projection preserves distinct source receipts and historical memory."""

from copy import deepcopy

import pytest
from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.reading_audit.evidence import current_catalog
from podgen.runtime.evaluation.reading_audit.memory import fingerprint, project
from podgen.writing.reading_audit import VERSION as AUDIT_VERSION
from podgen.writing.reading_audit.memory_projection import KEY, VERSION, from_manifest
from podgen.writing.reading_audit.registry import from_manifest as audit_from_manifest

from tests.test_reading_audit_evidence import catalog


def setup(monkeypatch):
    m, corpus, root, _, _, journal, index = catalog(monkeypatch)
    values = current_catalog(journal, root, m, index, corpus)
    return m, values, index


def test_packet_content_is_shared_but_full_read_and_original_persisted_proofs_remain(monkeypatch):
    m, current, index = setup(monkeypatch)
    previous = {"current:B1": deepcopy(current["B1"]), "current:E1": deepcopy(current["E1"])}
    frozen = deepcopy(previous)
    refs, retained, reused = project(previous, current, m, index, shared=True)
    assert refs == {
        "current:B1": dict(from_review="current", evidence_id="B1"),
        "current:E1": dict(from_review="previous", evidence_id="H1"),
    }
    assert retained == {"H1": previous["current:E1"]}
    assert reused == {"current:B1": "B1"}
    assert previous == frozen and current["B1"] == frozen["current:B1"]
    assert retained["H1"]["event_ref"] == frozen["current:E1"]["event_ref"]


@pytest.mark.parametrize("field", ["content", "ref", "sha256", "start", "key"])
def test_same_text_without_same_full_content_and_frozen_provenance_is_not_shared(monkeypatch, field):
    m, current, index = setup(monkeypatch)
    previous = dict(old=deepcopy(current["B1"]))
    changed = deepcopy(current)
    if field == "content":
        changed["B1"]["content"]["supports"] = ["another-card"]
        changed["B1"]["content_hash"] = digest(changed["B1"]["content"])
    else:
        changed["B1"]["source_selectors"][0][field] = "different" if field != "start" else 1
    refs, retained, reused = project(previous, changed, m, index, shared=True)
    assert refs["old"] == dict(from_review="previous", evidence_id="H1")
    assert retained["H1"] == previous["old"] and reused == {}


def test_renumbered_current_b_is_used_and_repeated_previous_packet_aliases_share_it(monkeypatch):
    m, values, index = setup(monkeypatch)
    old = deepcopy(values["B1"])
    previous = {"previous:H7": old, "current:B1": deepcopy(old)}
    now = {"B9": dict(deepcopy(values["B1"]), original_id="B9")}
    refs, retained, reused = project(previous, now, m, index, shared=True)
    assert all(row == dict(from_review="current", evidence_id="B9") for row in refs.values())
    assert retained == {} and reused == {"previous:H7": "B9", "current:B1": "B9"}


def test_only_identical_retained_receipts_share_an_h_alias(monkeypatch):
    m, current, index = setup(monkeypatch)
    read = current["E1"]
    previous = {
        "previous:H1": deepcopy(read),
        "current:E1": deepcopy(read),
        "current:E2": dict(
            deepcopy(read), event_ref=dict(record_kind="tool_execution", record_id="distinct-receipt")
        ),
    }
    refs, retained, _ = project(previous, current, m, index, shared=True)
    assert refs["previous:H1"] == refs["current:E1"]
    assert refs["current:E2"] != refs["current:E1"]
    assert len(retained) == 2


def test_missing_pin_preserves_the_exact_old_alias_order_and_full_evidence(monkeypatch):
    m, current, index = setup(monkeypatch)
    previous = {
        "current:B1": deepcopy(current["B1"]),
        "previous:H1": deepcopy(current["B1"]),
        "current:E1": deepcopy(current["E1"]),
    }
    refs, retained, reused = project(previous, current, m, index, shared=False)
    assert refs == {
        key: dict(from_review="previous", evidence_id=f"H{n}") for n, key in enumerate(previous, 1)
    }
    assert list(retained.values()) == list(previous.values()) and reused == {}
    assert fingerprint(current) == fingerprint(
        {key: dict(row, request_ref="new-request") for key, row in current.items()}
    )
    changed = deepcopy(current)
    changed["B1"]["content"]["text"] += "changed"
    assert fingerprint(changed) != fingerprint(current)


def test_memory_projection_pin_is_explicit_and_requires_the_audit_contract():
    assert from_manifest({}) is None
    assert from_manifest({KEY: VERSION, "reading_audit_contract": AUDIT_VERSION}) == VERSION
    for manifest in ({KEY: VERSION}, {KEY: "future", "reading_audit_contract": AUDIT_VERSION}):
        with pytest.raises(ContractError, match="unsupported_reading_audit_memory_projection"):
            audit_from_manifest(manifest)
