"""Production-only evidence views from the complete, frozen editorial materials."""

from podgen.domain.errors import ContractError

from .card_catalog import card_id
from .projection_versions import V9, manifest_version
from .source_bibliography import bibliography as bibliography

KEY = "fact_review_context"
VERSION = "contextual-evidence/1"
SIGNATURE_VERSION = "contextual-evidence/2"
SCHEMA_VERSION = "fact-revision/3"
READ_SCHEMA_VERSION = "fact-revision/4"


def validate(value, projection):
    if value is not None and (value not in (VERSION, SIGNATURE_VERSION) or projection != V9):
        raise ContractError("unsupported_fact_review_context")
    return value


def from_manifest(manifest):
    value = validate(manifest.get(KEY), manifest_version(manifest))
    if value is not None and (
        manifest.get("behavior_profile") != "production/1"
        or manifest.get("fact_review_schema_version")
        not in (SCHEMA_VERSION, READ_SCHEMA_VERSION, "fact-revision/5")
    ):
        raise ContractError("unsupported_fact_review_context")
    return value


def research_cautions(packet):
    if packet.contract == "research-editorial-input/1":
        return [
            dict(note=note, source_id=source.source_id)
            for source in packet.sources
            for note in source.limitations
        ]
    notes = {}
    for card in packet.dossier.knowledge_cards:
        for text in card.limitations:
            value = notes.setdefault(text, dict(note=text, supports=[]))
            identity = card_id(packet, card)
            if identity not in value["supports"]:
                value["supports"].append(identity)
    for text in packet.dossier.throughline_constraints:
        notes.setdefault(text, dict(note=text, supports=[]))["dossier_wide"] = True
    return list(notes.values())


def evidence_views(packet):
    documents = {source.key: source for source in packet.documents}
    primary, context = {}, {}
    for card in packet.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            source = documents[excerpt.document]
            target = primary if source.purpose == "primary" else context
            row = target.setdefault(
                (excerpt.document, excerpt.start, excerpt.end),
                dict(source=source.title, text=excerpt.text, supports=[]),
            )
            identity = card_id(packet, card)
            if identity not in row["supports"]:
                row["supports"].append(identity)
    return list(primary.values()), list(context.values())


def evidence_for_materials(materials, contract):
    """The same canonical packet rows supply delivery and lossless audit-memory matching."""
    if materials.packet.contract == "research-editorial-input/1":
        return [], []
    if contract is None:
        from .projections import evidence_view

        return evidence_view(materials.packet), []
    primary, context = evidence_views(materials.packet)
    if contract == SIGNATURE_VERSION:
        from .source_signatures import signature_evidence

        primary.extend(signature_evidence(materials))
    return primary, context
