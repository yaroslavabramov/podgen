"""Immutable exact-byte source admission for new editorial assignments."""

from io import BytesIO
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError

from .runtime_records import append_runtime

PURPOSES = ("brief", "dossier", "primary", "fixed_plan")


def import_document(repo, project, raw, *, source_path, purpose, scope_key):
    if purpose not in PURPOSES or not scope_key or not source_path or not raw.decode("utf-8").strip():
        raise ContractError("invalid_editorial_document")
    blob = repo.save_blob(project, BytesIO(raw), "text/plain")
    payload = dict(
        contract="editorial-source/1",
        source_path=source_path,
        purpose=purpose,
        scope_key=scope_key,
        content_blob=blob.model_dump(mode="json"),
    )
    ref = RuntimeRef(
        record_kind="user_input", record_id=uuid5(project, "editorial-source:" + digest(payload))
    )
    with repo.database.transaction() as tx:
        repo._lock(tx, project)
        append_runtime(repo, tx, project, ref, payload, replay=True)
    return ref


def read_admitted(repo, project, ref, payload, *, path, checksum, purpose):
    if (
        set(payload) != {"contract", "source_path", "purpose", "scope_key", "content_blob"}
        or payload["contract"] != "editorial-source/1"
    ):
        raise AccessDenied()
    expected = uuid5(project, "editorial-source:" + digest(payload))
    blob = BlobRef.model_validate(payload["content_blob"])
    if (
        ref.record_kind != "user_input"
        or ref.record_id != expected
        or payload["purpose"] != purpose
        or payload["source_path"] != path
        or blob.sha256 != checksum
    ):
        raise AccessDenied()
    return repo.read_runtime_blob(project, ref, blob), ("editorial-source/1", payload["scope_key"])
