"""Resolve cited aliases to actual deliveries and immutable source selectors before persistence."""

from copy import deepcopy
from hashlib import sha256

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.model_references import context_references
from podgen.writing.source_tools import SourceCorpus

from .. import model_turns
from ..request_payload import request_payload
from ..source_tool_recovery import lineage
from ..source_tool_trace import original_invocation


def snapshots(materials):
    loaded = {doc.path: doc for doc in materials.documents}
    result = []
    for source in materials.packet.documents:
        doc = loaded.get(source.source_path)
        if (
            doc is None
            or source.sha256 != doc.sha256
            or sha256(doc.text.encode()).hexdigest() != source.sha256
        ):
            raise ContractError("audit_source_snapshot_mismatch")
        result.append(
            dict(
                key=source.key,
                source=source.title,
                purpose=source.purpose,
                sha256=source.sha256,
                ref=doc.ref.model_dump(mode="json"),
                text=doc.text,
            )
        )
    return result


def validate_retained(value, materials, snapshot_index=None):
    available = (
        snapshot_index if snapshot_index is not None else {row["key"]: row for row in snapshots(materials)}
    )
    for selector in value["source_selectors"]:
        doc = available.get(selector["key"])
        if doc is None or any(doc[k] != selector[k] for k in ("sha256", "ref")):
            raise ContractError("audit_evidence_source_changed")
        if (
            not 0 <= selector["start"] < selector["end"] <= len(doc["text"])
            or doc["text"][selector["start"] : selector["end"]] != value["content"]["text"]
        ):
            raise ContractError("audit_evidence_selector_changed")
    if not value["source_selectors"] or value["content_hash"] != digest(value["content"]):
        raise ContractError("audit_evidence_binding_mismatch")


def _selectors(content, materials, snapshot_index=None):
    result = []
    for doc in snapshot_index.values() if snapshot_index is not None else snapshots(materials):
        if doc["source"] != content["source"]:
            continue
        start = doc["text"].find(content["text"])
        while start >= 0:
            result.append(
                dict(
                    key=doc["key"],
                    sha256=doc["sha256"],
                    ref=doc["ref"],
                    start=start,
                    end=start + len(content["text"]),
                )
            )
            start = doc["text"].find(content["text"], start + 1)
    if not result:
        raise ContractError("audit_evidence_not_in_frozen_sources")
    return result


def current_catalog(journal, invocation, materials, snapshot_index=None, corpus=None):
    root = original_invocation(journal, invocation)
    request_ref = RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"])
    request = journal.read(request_ref)
    payload = request_payload(journal.request(root))
    references = context_references(request["context"])
    result = {}
    for item in payload["audit_context"].get("packet_evidence_index", []):
        field, index = item["path"].strip("/").split("/")
        if field not in ("evidence", "context_excerpts"):
            raise ContractError("audit_packet_evidence_path_invalid")
        value = payload[field][int(index)]
        content = references.translate(value, inbound=True) if references else deepcopy(value)
        result[item["evidence_id"]] = dict(
            original_id=item["evidence_id"],
            kind="packet_evidence",
            content=content,
            content_hash=digest(content),
            source_selectors=_selectors(content, materials, snapshot_index),
            request_ref=request_ref.model_dump(mode="json"),
            origin_invocation_id=str(root["invocation_id"]),
        )
    corpus = corpus or SourceCorpus(materials)
    for ancestor in lineage(journal, root):
        turns = {row["turn_index"]: row for row in model_turns.rows(journal, ancestor)}
        for record in model_turns.events(journal, ancestor, kinds={"source_tool_result"}):
            data = record["data"]
            if (
                data.get("name") != "read_source"
                or "passage" not in data["result"]
                or "error" in data["result"]
            ):
                continue
            turn = turns.get(data.get("turn_index"))
            if (
                turn is None
                or turn["status"] != "response_completed"
                or record["invocation_id"] != str(ancestor["invocation_id"])
                or RuntimeRef.model_validate(record["request_ref"]).record_id != ancestor["request_record_id"]
            ):
                raise ContractError("audit_source_read_provenance_mismatch")
            passage = data["result"]["passage"]
            source = corpus.documents.get(passage["source_id"])
            if source is None:
                raise ContractError("audit_source_read_unknown")
            locator = f"{passage['source_id']}:{source.digest}:{passage['start']}:{passage['end']}"
            content = corpus.locate(locator)
            if content != {k: v for k, v in passage.items() if k != "read_id"}:
                raise ContractError("audit_source_read_receipt_mismatch")
            alias = passage["read_id"]
            if alias in result:
                raise ContractError("audit_duplicate_evidence_alias")
            result[alias] = dict(
                original_id=alias,
                kind="source_read",
                content=content,
                content_hash=digest(content),
                source_selectors=_selectors(content, materials, snapshot_index),
                locator=locator,
                event_ref=RuntimeRef(record_kind="tool_execution", record_id=record["_record_id"]).model_dump(
                    mode="json"
                ),
                request_ref=RuntimeRef(
                    record_kind="tool_execution", record_id=turn["request_record_id"]
                ).model_dump(mode="json"),
                root_request_ref=record["request_ref"],
                physical_invocation_id=str(turn["invocation_id"]),
                provider_response_id=turn["provider_response_id"],
                origin_invocation_id=str(ancestor["invocation_id"]),
            )
    return result


def resolve(output, current, previous, materials, snapshot_index=None):
    resolved = {}
    for claim in output["audit"]["claim_checks"]:
        seen = set()
        for ref in claim["source_refs"]:
            key = (ref["from_review"], ref["evidence_id"])
            if key in seen:
                raise ContractError("audit_duplicate_evidence_reference")
            seen.add(key)
            table = current if ref["from_review"] == "current" else previous
            if ref["evidence_id"] not in table:
                raise ContractError("audit_evidence_not_delivered")
            value = table[ref["evidence_id"]]
            validate_retained(value, materials, snapshot_index)
            resolved[":".join(key)] = deepcopy(value)
    return resolved
