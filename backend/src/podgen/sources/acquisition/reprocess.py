"""Explicit local re-extraction of a saved version, with no network dependency."""

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError
from podgen.persistence.artifact_preparation import verified_bytes
from podgen.sources.extraction.extract import DocumentExtractors

from .contracts import AcquisitionRequest, AcquisitionResult, FetchedDocument, ReExtractionRequest
from .profile import load_profile
from .stages import extract_document
from .store import AcquisitionStore


async def reprocess_source(repo, project_id, request, *, extractors=None):
    request = ReExtractionRequest.model_validate(request)
    if request.source_revision_ref.kind != "SourceRevision" or not request.reason.strip():
        raise ContractError("source_reextraction_requires_source_and_reason")
    store = AcquisitionStore(repo, project_id, request.operation_id)
    with store.lock():
        profile = load_profile(repo, project_id, request.profile_ref)
        source_revision = repo.get(project_id, request.source_revision_ref)
        source = source_revision.payload
        store.put("request", request.model_dump(mode="json"))
        saved = store.get("result")
        if saved:
            return AcquisitionResult.model_validate(saved)
        body = repo.read_blob(
            project_id, request.source_revision_ref, BlobRef.model_validate(source["original_blob"])
        )
        from podgen.domain.contracts.common import RuntimeRef

        acquisition_ref = RuntimeRef.model_validate(source["acquisition_record_ref"])
        acquisition = repo.get_runtime(project_id, acquisition_ref)
        headers = acquisition.get("headers", {"content-type": source["response_metadata"]["content_type"]})
        role = acquisition.get("role", "original")
        document = FetchedDocument(
            source["requested_uri"],
            source["resolved_uri"],
            acquisition.get("redirect_chain", []),
            source["response_metadata"]["status"],
            headers,
            body,
            source["acquired_at"],
            source["acquisition_kind"],
            source["limitations"],
        )
        adapter_request = AcquisitionRequest(
            operation_id=request.operation_id,
            source_id=source["source_id"],
            url=source["requested_uri"],
            profile_ref=request.profile_ref,
            rights=source["rights"],
            language=request.language or source.get("language") or "und",
        )
        result_source = await extract_document(
            store,
            adapter_request,
            profile,
            role,
            document,
            source_revision,
            extractors or DocumentExtractors(),
            acquisition_ref=acquisition_ref,
            original_proof=verified_bytes(
                repo, project_id, BlobRef.model_validate(source["original_blob"]), body
            ),
        )
        status = (
            "available"
            if result_source.status == "available"
            else "unconfirmed"
            if result_source.status == "unconfirmed"
            else "unavailable"
        )
        result = AcquisitionResult(
            acquisition_record_ref=store.ref("request"),
            sources=[result_source],
            status=status,
            diagnostics=[],
        )
        store.put("result", result.model_dump(mode="json"))
        return result
