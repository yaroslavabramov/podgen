"""Share only identical frozen packet evidence; keep original read receipts in full."""

from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.writing.fact_context import evidence_for_materials, from_manifest
from podgen.writing.reading_audit.input import packet_index

from .evidence import packet_catalog, validate_retained

PACKET_FIELDS = ("original_id", "kind", "content", "content_hash", "source_selectors")


def fingerprint(catalog):
    return digest(
        {
            key: {field: row[field] for field in PACKET_FIELDS}
            for key, row in catalog.items()
            if row["kind"] == "packet_evidence"
        }
    )


def current_packet(materials, manifest, snapshot_index):
    primary, context = evidence_for_materials(materials, from_manifest(manifest))
    payload = dict(evidence=primary, context_excerpts=context)
    payload["audit_context"] = dict(packet_evidence_index=packet_index(payload))
    return packet_catalog(payload, materials, snapshot_index)


def identity(value):
    return digest({key: value[key] for key in ("content", "content_hash", "source_selectors")})


def project(previous, current, materials, snapshot_index, *, shared):
    packet_matches = {}
    if shared:
        for alias, value in current.items():
            if value["kind"] == "packet_evidence":
                packet_matches.setdefault(identity(value), alias)
    refs, retained, reused, receipts = {}, {}, {}, {}
    for key, value in previous.items():
        validate_retained(value, materials, snapshot_index)
        match = packet_matches.get(identity(value)) if shared and value["kind"] == "packet_evidence" else None
        if match is not None:
            refs[key] = dict(from_review="current", evidence_id=match)
            reused[key] = match
            continue
        proof = digest(value)
        alias = receipts.get(proof) if shared else None
        if alias is None:
            alias = f"H{len(retained) + 1}"
            retained[alias] = deepcopy(value)
            receipts[proof] = alias
        refs[key] = dict(from_review="previous", evidence_id=alias)
    return refs, retained, reused
