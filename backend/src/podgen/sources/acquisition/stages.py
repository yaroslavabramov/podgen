"""Durable per-document fetch and extraction stages."""

import httpx

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.persistence.artifact_preparation import verified_bytes
from podgen.sources.extraction.contracts import ExtractedDocument

from .contracts import AcquiredSource
from .persistence import load_original, save_original, save_source
from .representation import save_representation


async def acquire_document(store, request, profile, role, url, fetcher, extractors, *, headers=None):
    key = role + ":outcome"
    records = store.get_many(
        [role + ":" + suffix for suffix in ("outcome", "original", "source", "extracted", "representation")]
    )
    saved = records[key]
    if saved is not None:
        return AcquiredSource.model_validate(saved["source"]) if saved["source"] else None
    original = load_original(store, role, records[role + ":original"])
    if original is None:
        from podgen.runtime.dispatch import before_external

        before_external("source_fetch", f"{request.operation_id}:{role}")
        try:
            document = await fetcher.fetch(url, profile, headers=headers)
        except (ValueError, RuntimeError, OSError, TimeoutError, ImportError, httpx.HTTPError) as error:
            store.put(key, dict(source=None, diagnostics=[type(error).__name__ + ":" + str(error)]))
            return None
        original = save_original(store, request, role, document)
    source = save_source(store, request, role, original, records[role + ":source"])
    return await extract_document(
        store,
        request,
        profile,
        role,
        original.document,
        source,
        extractors,
        acquisition_ref=store.ref(role + ":original"),
        records=records,
        original_proof=original.verified,
    )


async def extract_document(
    store,
    request,
    profile,
    role,
    document,
    source,
    extractors,
    *,
    acquisition_ref,
    records=None,
    original_proof=None,
):
    key = role + ":outcome"
    if records is None:
        records = store.get_many(
            [role + ":" + suffix for suffix in ("outcome", "extracted", "representation")]
        )
    saved = records[key]
    if saved is not None:
        return AcquiredSource.model_validate(saved["source"])
    try:
        extracted_record = records[role + ":extracted"]
        if extracted_record is None:
            from podgen.runtime.dispatch import before_external

            before_external("source_extract", f"{request.operation_id}:{role}")
            extracted = await extractors.extract(document, profile, role=role)
            blob = store.blob(extracted.text.encode("utf-8"), "text/plain; charset=utf-8")
            extracted_record = dict(
                text_blob=blob.model_dump(mode="json"),
                metadata={k: v for k, v in extracted.__dict__.items() if k != "text"},
            )
            store.put(role + ":extracted", extracted_record)
        else:
            extracted = ExtractedDocument(
                text=store.bytes(
                    role + ":extracted", BlobRef.model_validate(extracted_record["text_blob"])
                ).decode("utf-8"),
                **extracted_record["metadata"],
            )
        text_blob = BlobRef.model_validate(extracted_record["text_blob"])
        text_proof = verified_bytes(store.repo, store.project, text_blob, extracted.text.encode("utf-8"))
        represented = save_representation(
            store,
            request,
            role,
            source,
            document,
            extracted,
            profile,
            text_proof,
            original_proof,
            acquisition_ref,
            records[role + ":representation"],
        )
        return AcquiredSource.model_validate(represented["source"])
    except ContractError:
        raise
    except (ValueError, RuntimeError, OSError, TimeoutError, ImportError) as error:
        result = AcquiredSource(
            source_revision_ref=source.ref,
            representation_refs=[],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status="extraction_failed",
            diagnostics=[type(error).__name__ + ":" + str(error)],
        )
    store.put(key, dict(source=result.model_dump(mode="json")))
    return result


def saved_extracted_text(store, source):
    ref = source.representation_refs[0]
    payload = store.repo.get(store.project, ref).payload
    blob = BlobRef.model_validate(payload["canonical_text_blob"])
    return store.repo.read_blob(store.project, ref, blob).decode("utf-8")
