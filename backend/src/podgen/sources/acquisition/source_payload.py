"""Build source metadata without publishing an intermediate editorial artifact."""

from uuid import uuid5

from podgen.domain.contracts.evidence import SourceDate, SourceRevision


def received(request, role, document, original_blob, acquisition_ref):
    limitations = list(document.limitations)
    if role != "original":
        limitations.append("separately_acquired_" + role + "_not_the_reported_page_bytes")
    return SourceRevision(
        source_id=request.source_id if role == "original" else uuid5(request.source_id, role),
        original_blob=original_blob,
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
        acquisition_record_ref=acquisition_ref,
        rights=request.rights,
        source_metadata_evidence=[dict(reported_url=request.url, acquisition_role=role)],
        limitations=limitations,
    ).model_dump(mode="json")


def enriched(payload, extracted, extraction_ref, *, prior=None):
    if not extracted.title and not extracted.published_date:
        return payload
    title_changed = extracted.title is not None and extracted.title != payload.get("title")
    date_changed = extracted.published_date is not None and extracted.published_date != (
        payload.get("published_at") or {}
    ).get("value")
    if not title_changed and not date_changed:
        return payload
    changes = {}
    evidence = dict(extractor=extracted.name, extraction_record_ref=extraction_ref.model_dump(mode="json"))
    if prior is not None:
        evidence["metadata_source_revision_ref"] = prior.model_dump(mode="json")
    if title_changed:
        changes["title"] = extracted.title
        evidence["title"] = extracted.title
    if date_changed:
        changes["published_at"] = SourceDate(
            value=extracted.published_date, precision="unknown", basis="reported_by_" + extracted.name
        ).model_dump(mode="json")
        evidence["published_date"] = extracted.published_date
    return SourceRevision.model_validate(
        payload
        | changes
        | dict(
            source_metadata_evidence=payload["source_metadata_evidence"] + [evidence],
        )
    ).model_dump(mode="json")
