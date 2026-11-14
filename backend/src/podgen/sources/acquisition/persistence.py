"""Save each received original before creating any extracted representation."""

from dataclasses import asdict, dataclass

from podgen.domain.contracts.common import BlobRef
from podgen.persistence.artifact_preparation import VerifiedBlob, verified_bytes

from .contracts import FetchedDocument


@dataclass(frozen=True)
class ReceivedOriginal:
    document: FetchedDocument
    record: dict
    verified: VerifiedBlob


def save_original(store, request, role, document):
    key = role + ":original"
    saved = {}

    def build(blob):
        saved.update(
            dict(
                contract="source-received-original/1",
                request_ref=store.ref("request").model_dump(mode="json"),
                role=role,
                original_blob=blob.model_dump(mode="json"),
                **{k: v for k, v in asdict(document).items() if k != "body"},
            )
        )
        return saved

    store.receipt(key, document.body, document.headers.get("content-type", "application/octet-stream"), build)
    blob = BlobRef.model_validate(saved["original_blob"])
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
