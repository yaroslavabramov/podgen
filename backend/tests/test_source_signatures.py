"""Signatures are local primary evidence, not initials expansion or document-wide name search."""

import hashlib
from types import SimpleNamespace

import pytest
from podgen.domain.errors import ContractError
from podgen.writing.source_signatures import signature_evidence


def material(text, quote, *, cards=("C1",)):
    start = text.index(quote)
    excerpt = SimpleNamespace(document="D1", start=start, end=start + len(quote), text=quote)
    packet = SimpleNamespace(
        contract="editorial-input/2",
        documents=[
            SimpleNamespace(
                key="D1",
                title="Exact source",
                source_path="private/source.txt",
                purpose="primary",
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        ],
        dossier=SimpleNamespace(
            knowledge_cards=[SimpleNamespace(card_id=identity, excerpts=[excerpt]) for identity in cards]
        ),
    )
    return SimpleNamespace(
        packet=packet,
        documents=[SimpleNamespace(path="private/source.txt", sha256=packet.documents[0].sha256, text=text)],
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_partial_line_end_keeps_signature_verbatim_without_repeating_excerpt(newline):
    closing = "The complete closing statement, with its meaning intact."
    signature = "  ALICE EXAMPLE, M.A."
    text = newline.join(("Earlier text.", closing, "", signature, "A later unrelated paragraph."))
    materials = material(text, "complete closing statem", cards=("C1", "C2"))
    assert signature_evidence(materials) == [
        dict(source="Exact source", text=signature + newline, supports=["C1", "C2"])
    ]


@pytest.mark.parametrize("gap", ["Another substantive sentence.\n", "=== PDF PAGE 9 ===\n", "\n\n\n"])
def test_intervening_text_or_distant_signature_never_gets_attached(gap):
    text = "The cited closing sentence.\n" + gap + "ALICE EXAMPLE, M.A.\n"
    assert signature_evidence(material(text, "The cited closing sentence.")) == []


@pytest.mark.parametrize(
    "line",
    [
        "SUMMARY AND CONCLUSIONS",
        "Alice Example",
        "ALICE EXAMPLE",
        "Alice Example, M.A.",
        "ALICE EXAMPLE, M.A., followed by a different statement.",
    ],
)
def test_unknown_or_unsigned_name_shapes_do_not_become_attribution(line):
    assert signature_evidence(material("A complete claim.\n" + line + "\n", "A complete claim.")) == []


def test_initials_are_never_expanded_and_missing_sources_remain_missing():
    value = material("A complete claim.\nH. EXAMPLE, M.A.", "A complete claim.\n")
    assert signature_evidence(value)[0]["text"] == "H. EXAMPLE, M.A."
    value.documents = []
    assert signature_evidence(value) == []


def test_changed_source_or_excerpt_offsets_fail_explicitly():
    value = material("A complete claim.\nALICE EXAMPLE, M.A.\n", "A complete claim.")
    value.packet.dossier.knowledge_cards[0].excerpts[0].end -= 1
    with pytest.raises(ContractError, match="fact_signature_excerpt_mismatch"):
        signature_evidence(value)
    value.documents[0].text += "Changed bytes."
    with pytest.raises(ContractError, match="fact_signature_source_mismatch"):
        signature_evidence(value)
