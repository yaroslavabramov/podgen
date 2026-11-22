"""Stable native reference tables, scoped prose rebinding and untouched historical projection."""

from copy import deepcopy
from uuid import uuid4

from podgen.domain.canonical import digest
from podgen.runtime.evaluation.reading_audit.memory import project
from podgen.runtime.evaluation.reading_audit.reference_text import FIELDS, fields, rebind, text_aliases
from tests.test_reading_audit_memory import setup


def another_read(value):
    result = deepcopy(value)
    result["original_id"] = "E1"
    result["event_ref"]["record_id"] = str(uuid4())
    result["content"]["text"] = result["content"]["text"][1:]
    result["content"]["start"] += 1
    result["source_selectors"][0]["start"] += 1
    result["content_hash"] = digest(result["content"])
    parts = result["locator"].split(":")
    parts[-2] = str(int(parts[-2]) + 1)
    result["locator"] = ":".join(parts)
    return result


def test_old_h_new_e_and_shared_b_never_exchange_meanings_or_reuse_retired_numbers(monkeypatch):
    materials, current, index = setup(monkeypatch)
    original = current["E1"]
    new = another_read(original)
    previous = {"current:E1": new, "previous:H12": original, "current:B1": current["B1"]}
    frozen = deepcopy(previous)
    aliases, retained, reused = project(previous, current, materials, index, shared=True, previous_max=20)
    assert aliases == {
        "current:E1": dict(from_review="previous", evidence_id="H21"),
        "previous:H12": dict(from_review="previous", evidence_id="H12"),
        "current:B1": dict(from_review="current", evidence_id="B1"),
    }
    assert retained["H12"] == original and retained["H21"] == new
    assert reused == {"current:B1": "B1"}
    assert previous == frozen
    # A later audit retires H21 and receives another native E1. The saved maximum
    # alone prevents reuse; source documents and receipts remain separate.
    next_aliases, next_retained, _ = project(
        {"previous:H12": original, "current:E1": another_read(original)},
        current,
        materials,
        index,
        shared=True,
        previous_max=21,
    )
    assert next_aliases["current:E1"]["evidence_id"] == "H22"
    assert next_retained["H12"] == original


def test_all_four_semantic_fields_use_the_native_table_but_quotes_and_anchors_do_not(monkeypatch):
    materials, current, index = setup(monkeypatch)
    previous = {
        "current:E1": another_read(current["E1"]),
        "previous:H12": current["E1"],
        "current:B1": current["B1"],
    }
    aliases, _, _ = project(previous, current, materials, index, shared=True, previous_max=20)
    replacements = text_aliases(previous, aliases)
    text = 'H12, E1 and B1; E999 stays unknown. «E1 H12» and "E1" are verbatim; `E1` is a reference.\n```text\nE1 H12\n``` ‘E1’'
    row = dict(
        {key: text for key in FIELDS},
        decision_id="D11",
        anchors=[dict(utterance_index=0, quote="E1 H12")],
        source_refs=[
            dict(from_review="previous", evidence_id="H12"),
            dict(from_review="current", evidence_id="E1"),
        ],
        other_text="E1 H12",
    )
    frozen = deepcopy(row)
    result = fields(row, replacements)
    expected = 'H12, H21 and B1; E999 stays unknown. «E1 H12» and "E1" are verbatim; `H21` is a reference.\n```text\nE1 H12\n``` ‘E1’'
    assert all(result[key] == expected for key in FIELDS)
    assert result["source_refs"] == row["source_refs"]
    assert result["anchors"] == row["anchors"] and result["other_text"] == row["other_text"]
    assert row == frozen
    assert current["E1"]["content"]["text"] == previous["previous:H12"]["content"]["text"]


def test_shared_packet_renumbering_uses_proven_identity_and_ambiguous_bare_numbers_are_not_guessed(
    monkeypatch,
):
    materials, current, index = setup(monkeypatch)
    previous = {"current:B1": current["B1"]}
    changed = {"B9": dict(deepcopy(current["B1"]), original_id="B9")}
    aliases, retained, _ = project(previous, changed, materials, index, shared=True, previous_max=0)
    assert text_aliases(previous, aliases) == {"B1": "B9"} and retained == {}
    assert (
        text_aliases(
            {"current:E1": current["E1"], "previous:E1": another_read(current["E1"])},
            {"current:E1": dict(evidence_id="H1"), "previous:E1": dict(evidence_id="H2")},
        )
        == {}
    )


def test_identical_packet_is_shared_even_if_previously_delivered_as_h(monkeypatch):
    materials, current, index = setup(monkeypatch)
    aliases, retained, reused = project(
        {"previous:H12": current["B1"]}, current, materials, index, shared=True, previous_max=0
    )
    assert aliases["previous:H12"] == dict(from_review="current", evidence_id="B1")
    assert reused == {"previous:H12": "B1"}
    assert retained == {}


def test_legacy_projection_and_unknown_text_are_unchanged(monkeypatch):
    materials, current, index = setup(monkeypatch)
    previous = {"current:E1": another_read(current["E1"]), "previous:H12": current["E1"]}
    aliases, _, _ = project(previous, current, materials, index, shared=True)
    assert aliases["current:E1"]["evidence_id"] == "H1"
    assert aliases["previous:H12"]["evidence_id"] == "H2"
    assert rebind("Unresolved H99, no invented source.", {}) == "Unresolved H99, no invented source."
