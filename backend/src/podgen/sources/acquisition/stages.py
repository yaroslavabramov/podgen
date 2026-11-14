"""Durable per-document fetch and extraction stages."""

import httpx

from podgen.domain.contracts.common import BlobRef
from podgen.domain.contracts.evidence import SourceDate, SourceRevision
from podgen.domain.errors import ContractError
from podgen.sources.extraction.contracts import ExtractedDocument

from .contracts import AcquiredSource
from .persistence import load_original, save_original, save_source
from .representation import save_representation


async def acquire_document(store, request, profile, role, url, fetcher, extractors, *, headers=None):
    key = role + ":outcome"
    saved = store.get(key)
    if saved is not None:
        return AcquiredSource.model_validate(saved["source"]) if saved["source"] else None
    document = load_original(store, role)
    if document is None:
        from podgen.runtime.dispatch import before_external

        before_external("source_fetch", f"{request.operation_id}:{role}")
        try:
            document = await fetcher.fetch(url, profile, headers=headers)
        except (ValueError, RuntimeError, OSError, TimeoutError, ImportError, httpx.HTTPError) as error:
            store.put(key, dict(source=None, diagnostics=[type(error).__name__ + ":" + str(error)]))
            return None
        document = save_original(store, request, role, document)
    source_ref = save_source(store, request, role, document)
    return await extract_document(
        store,
        request,
        profile,
        role,
        document,
        source_ref,
        extractors,
        acquisition_ref=store.ref(role + ":original"),
    )


async def extract_document(
    store, request, profile, role, document, source_ref, extractors, *, acquisition_ref
):
    key = role + ":outcome"
    saved = store.get(key)
    if saved is not None:
        return AcquiredSource.model_validate(saved["source"])
    try:
        extracted_record = store.get(role + ":extracted")
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
        source_ref = enrich_source(store, request, role, source_ref, extracted)
        represented = save_representation(store, request, role, source_ref, document, extracted, profile)
        result = AcquiredSource(
            source_revision_ref=source_ref,
            representation_refs=[represented["representation_ref"]],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status=represented["status"],
            diagnostics=represented["diagnostics"],
        )
    except ContractError:
        raise
    except (ValueError, RuntimeError, OSError, TimeoutError, ImportError) as error:
        result = AcquiredSource(
            source_revision_ref=source_ref,
            representation_refs=[],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status="extraction_failed",
            diagnostics=[type(error).__name__ + ":" + str(error)],
        )
    store.put(key, dict(source=result.model_dump(mode="json")))
    return result


def enrich_source(store, request, role, source_ref, extracted):
    if not extracted.title and not extracted.published_date:
        return source_ref
    source = store.repo.get(store.project, source_ref).payload
    date = None
    if extracted.published_date:
        date = SourceDate(
            value=extracted.published_date, precision="unknown", basis="reported_by_" + extracted.name
        )
    enriched = SourceRevision.model_validate(source).model_copy(
        update=dict(
            title=extracted.title,
            published_at=date,
            source_metadata_evidence=source["source_metadata_evidence"]
            + [
                dict(
                    metadata_source_revision_ref=source_ref.model_dump(mode="json"),
                    extractor=extracted.name,
                    title=extracted.title,
                    published_date=extracted.published_date,
                )
            ],
        )
    )
    return store.create(role + ":metadata-source", "SourceRevision", enriched.model_dump(mode="json"))


def saved_extracted_text(store, source):
    ref = source.representation_refs[0]
    payload = store.repo.get(store.project, ref).payload
    blob = BlobRef.model_validate(payload["canonical_text_blob"])
    return store.repo.read_blob(store.project, ref, blob).decode("utf-8")
