"""Save each received original before creating any extracted representation."""

from dataclasses import asdict
from uuid import uuid5

from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.contracts.evidence import SourceRevision

from .contracts import FetchedDocument


def save_original(store, request, role, document):
    key = role + ":original"
    saved = store.get(key)
    if saved is None:
        blob = store.blob(document.body, document.headers.get("content-type", "application/octet-stream"))
        saved = dict(
            contract="source-received-original/1",
            request_ref=store.ref("request").model_dump(mode="json"),
            role=role,
            original_blob=blob.model_dump(mode="json"),
            **{k: v for k, v in asdict(document).items() if k != "body"},
        )
        store.put(key, saved)
    return load_original(store, role)


def load_original(store, role):
    key = role + ":original"
    record = store.get(key)
    if record is None:
        return None
    blob = BlobRef.model_validate(record["original_blob"])
    fields = {key: record[key] for key in FetchedDocument.__dataclass_fields__ if key != "body"}
    return FetchedDocument(**fields, body=store.bytes(key, blob))


def save_source(store, request, role, document):
    key = role + ":source"
    saved = store.get(key)
    if saved:
        return ArtifactRef.model_validate(saved["source_revision_ref"])
    original = store.get(role + ":original")
    limitations = list(document.limitations)
    if role != "original":
        limitations.append("separately_acquired_" + role + "_not_the_reported_page_bytes")
    payload = SourceRevision(
        source_id=request.source_id if role == "original" else uuid5(request.source_id, role),
        original_blob=original["original_blob"],
        requested_uri=document.requested_url,
        resolved_uri=document.resolved_url,
        redirect_chain=[hop["resolved_target"] for hop in document.redirect_chain],
        acquired_at=document.acquired_at,
        response_metadata=dict(
            status=document.status_code,
            content_type=document.headers.get("content-type", "application/octet-stream"),
            etag=document.headers.get("etag"),
            last_modified=document.headers.get("last-modified"),
        ),
        creators=[],
        language=None if request.language == "und" else request.language,
        acquisition_kind=document.acquisition_kind,
        acquisition_record_ref=store.ref(role + ":original"),
        rights=request.rights,
        source_metadata_evidence=[dict(reported_url=request.url, acquisition_role=role)],
        limitations=limitations,
    )
    ref = store.create(key, "SourceRevision", payload.model_dump(mode="json"))
    store.put(key, dict(source_revision_ref=ref.model_dump(mode="json")))
    return ref
