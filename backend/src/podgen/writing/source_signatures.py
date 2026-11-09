"""Recognize an explicit signature next to an exact cited passage, never mine document names.

The conservative supported form is an uppercase personal name followed by abbreviated
credentials. Unknown forms stay unrecognized. Quote only the signature without normalizing OCR.
"""

import hashlib
import re

from podgen.domain.errors import ContractError

from .card_catalog import card_id

_CREDENTIAL = r"(?:[A-Z][a-z]?\.[ \t]*){2,}"
_SIGNATURE = re.compile(rf"(?P<name>[^,\r\n]+),[ \t]*{_CREDENTIAL}(?:,[ \t]*{_CREDENTIAL})*")


def _signature(line):
    match = _SIGNATURE.fullmatch(line.strip())
    if match is None:
        return False
    words = match["name"].split()
    return len(words) >= 2 and all(
        any(char.isalpha() for char in word)
        and all((char.isalpha() and char.isupper()) or char in "'’.-‐–" for char in word)
        for word in words
    )


def adjacent_signature(text, excerpt):
    if not 0 <= excerpt.start < excerpt.end <= len(text) or text[excerpt.start : excerpt.end] != excerpt.text:
        raise ContractError("fact_signature_excerpt_mismatch")
    last = excerpt.end - 1
    while last >= excerpt.start and text[last].isspace():
        last -= 1
    if last < excerpt.start:
        return None
    line_end = text.find("\n", last + 1)
    if line_end < 0:
        return None
    cursor = line_end + 1
    # At most two blank separator lines; any other intervening text ends adjacency.
    for _ in range(3):
        end = text.find("\n", cursor)
        end = len(text) if end < 0 else end + 1
        line = text[cursor:end]
        if line.strip():
            return (cursor, end) if _signature(line) else None
        if end == len(text):
            return None
        cursor = end
    return None


def signature_evidence(materials):
    documents = {source.key: source for source in materials.packet.documents if source.purpose == "primary"}
    loaded = {document.path: document for document in materials.documents}
    verified, result = {}, {}
    for card in materials.packet.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            source = documents.get(excerpt.document)
            document = loaded.get(source.source_path) if source is not None else None
            if document is None:
                continue
            if source.key not in verified:
                if (
                    document.sha256 != source.sha256
                    or hashlib.sha256(document.text.encode()).hexdigest() != source.sha256
                ):
                    raise ContractError("fact_signature_source_mismatch")
                verified[source.key] = document.text
            text = verified[source.key]
            span = adjacent_signature(text, excerpt)
            if span is None:
                continue
            row = result.setdefault(
                (source.key, *span), dict(source=source.title, text=text[span[0] : span[1]], supports=[])
            )
            identity = card_id(materials.packet, card)
            if identity not in row["supports"]:
                row["supports"].append(identity)
    return list(result.values())
