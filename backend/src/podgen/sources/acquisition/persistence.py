"""Save each received original before creating any extracted representation."""

from dataclasses import asdict, dataclass
from uuid import uuid5

from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.contracts.evidence import SourceRevision
from podgen.persistence.artifact_preparation import VerifiedBlob, prepare, verified_bytes

from .contracts import FetchedDocument


@dataclass(frozen=True)
class ReceivedOriginal:
    document: FetchedDocument
    record: dict
    verified: VerifiedBlob


def save_original(store, request, role, document):
    key = role + ":original"
    blob = store.blob(document.body, document.headers.get("content-type", "application/octet-stream"))
    saved = dict(
        contract="source-received-original/1",
        request_ref=store.ref("request").model_dump(mode="json"),
        role=role,
        original_blob=blob.model_dump(mode="json"),
        **{k: v for k, v in asdict(document).items() if k != "body"},
    )
    store.put(key, saved)
    return ReceivedOriginal(document, saved, verified_bytes(store.repo, store.project, blob, document.body))


def load_original(store, role, record):
    key = role + ":original"
    if record is None:
        return None
    blob = BlobRef.model_validate(record["original_blob"])
    fields = {key: record[key] for key in FetchedDocument.__dataclass_fields__ if key != "body"}
    body = store.bytes(key, blob)
    return ReceivedOriginal(
        FetchedDocument(**fields, body=body), record, verified_bytes(store.repo, store.project, blob, body)
    )


def save_source(store, request, role, original, saved):
    key = role + ":source"
    if saved:
        return store.repo.get(store.project, ArtifactRef.model_validate(saved["source_revision_ref"]))
    document = original.document
    limitations = list(document.limitations)
    if role != "original":
        limitations.append("separately_acquired_" + role + "_not_the_reported_page_bytes")
    payload = SourceRevision(
        source_id=request.source_id if role == "original" else uuid5(request.source_id, role),
        original_blob=original.record["original_blob"],
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
    body = payload.model_dump(mode="json")
    prepared = prepare(store.repo, store.project, "SourceRevision", body, verified=[original.verified])

    def publish(publication):
        source = publication.create(key, "SourceRevision", body)
        publication.put(key, dict(source_revision_ref=source.ref.model_dump(mode="json")))
        return source

    return store.publication(publish, prepared=prepared)
