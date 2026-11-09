"""Explicit frozen-source metadata, not a general metadata inference or article summarizer.

Recognize labelled headers, JSON metadata and a conservative publisher-header layout.
Unknown layouts stay unavailable; no body-wide author/DOI search or partial field output.
"""

import hashlib
import json
import re

from podgen.domain.errors import ContractError

from .card_catalog import card_id

_LABEL = re.compile(r"^(?:Title|Authors?|Published|Publication|DOI|Journal|Citation):\s*\S", re.I)
_BYLINE = re.compile(r"^[A-Z][^:;!?]*[0-9¹²³⁴⁵⁶⁷⁸⁹]\s*&\s*[A-Z][^:;!?]*[0-9¹²³⁴⁵⁶⁷⁸⁹]\s*$")
_METADATA = ("title", "author", "authors", "datePublished", "published", "journal", "doi", "DOI")


def bibliographic_excerpts(text):
    lines = text.splitlines(keepends=True)
    labelled = []
    for line in lines:
        if not line.strip():
            continue
        if not _LABEL.match(line):
            break
        labelled.append(line)
    if labelled:
        return labelled
    if len(lines) < 4 or lines[0].strip() not in ("ARTICLE", "REVIEW"):
        return []
    if not (lines[1].startswith("Received ") and "Accepted " in lines[1] and "Published " in lines[1]):
        return []
    # This layout must identify a complete header, not a possible author line later in the body.
    byline = next(
        (i for i in range(3, min(len(lines), 8)) if _BYLINE.fullmatch(lines[i].rstrip("\r\n"))), None
    )
    if byline is None or any(re.search(r"[.!?]", line) for line in lines[2:byline]):
        return []
    result = ["".join(lines[: byline + 1])]
    doi_line = next((line for line in lines[:32] if line.startswith("DOI:")), None)
    doi_match = re.match(r"DOI:\s*(\S+)", doi_line) if doi_line else None
    if doi_match:
        doi = doi_match.group(1)
        publication = next((line for line in lines[:32] if "|" in line and f"DOI: {doi}" in line), doi_line)
        result.append(publication)
    return result


def _structured(text):
    if not text.lstrip().startswith("{"):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}

    def names(item):
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            return [name for child in item if (name := names(child))]
        if isinstance(item, dict):
            return {
                key: item[key]
                for key in ("name", "givenName", "familyName")
                if isinstance(item.get(key), str)
            }
        return None

    result = {}
    for key in _METADATA:
        item = value.get(key)
        if key in ("author", "authors", "journal"):
            item = names(item)
        elif not isinstance(item, (str, int)) or isinstance(item, bool):
            continue
        if isinstance(item, (str, list, dict, int)) and not isinstance(item, bool) and item:
            result[key] = item
    return result


def bibliography(materials):
    loaded = {document.path: document for document in materials.documents}
    result = []
    for source in materials.packet.documents:
        if source.purpose != "primary":
            continue
        item = dict(
            source=source.title,
            supports=[
                card_id(materials.packet, card)
                for card in materials.packet.dossier.knowledge_cards
                if any(excerpt.document == source.key for excerpt in card.excerpts)
            ],
        )
        document = loaded.get(source.source_path)
        excerpts, metadata = [], {}
        if document is not None:
            if (
                document.sha256 != source.sha256
                or hashlib.sha256(document.text.encode()).hexdigest() != source.sha256
            ):
                raise ContractError("fact_bibliography_source_mismatch")
            excerpts, metadata = bibliographic_excerpts(document.text), _structured(document.text)
        item["bibliographic_evidence"] = excerpts
        if metadata:
            item["bibliographic_metadata"] = metadata
        item["bibliography_status"] = "available_evidence" if excerpts or metadata else "not_available"
        result.append(item)
    return result
