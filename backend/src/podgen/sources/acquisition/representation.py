"""Persist full extractor output and exact text ranges, with no inferred original anchors."""

import re
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.contracts.evidence import SourceDate, SourceRepresentation, SourceRevision
from podgen.persistence.artifact_preparation import prepare
from podgen.sources.extraction.quality import assess_web_fetch_health

from .contracts import AcquiredSource


def availability(document, extracted, profile):
    health = assess_web_fetch_health(extracted.text, min_chars=profile.min_text_chars)
    reasons = list(health.reasons)
    if document.status_code >= 400:
        reasons.append("http_status_" + str(document.status_code))
    from urllib.parse import urlparse

    requested, final = urlparse(document.requested_url), urlparse(document.resolved_url)
    segments = {x.casefold() for x in final.path.split("/") if x}
    access = {
        "auth",
        "authenticate",
        "authorization",
        "authorize",
        "consent",
        "cookie-consent",
        "login",
        "sign-in",
        "signin",
        "sso",
    }
    host_access = {"accounts", "auth", "idp", "identity", "login", "signin", "sso"}
    changed = (requested.hostname, requested.path) != (final.hostname, final.path)
    if changed and (
        segments & access
        or (requested.hostname != final.hostname and set((final.hostname or "").split(".")) & host_access)
    ):
        reasons.append("redirect_to_access_page")
        return "unavailable", reasons
    state = {"ok": "available", "blocked": "unavailable", "low_signal": "unconfirmed"}[health.state]
    if document.status_code >= 400 and state != "available":
        state = "unavailable"
    return state, reasons


def save_representation(
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
    saved,
):
    key = role + ":representation"
    if saved:
        previous = store.repo.get(store.project, ArtifactRef.model_validate(saved["representation_ref"]))
        result = AcquiredSource(
            source_revision_ref=previous.payload["source_revision_ref"],
            representation_refs=[previous.ref],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status=saved["status"],
            diagnostics=saved["diagnostics"],
        )
        value = dict(source=result.model_dump(mode="json"))
        store.put(role + ":outcome", value)
        return value
    state, reasons = availability(document, extracted, profile)
    text_blob = text_proof.ref
    # One range per paragraph, in Python Unicode code points used by TextSelector.
    blocks = []
    for i, match in enumerate(re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|$)", extracted.text, re.DOTALL)):
        blocks.append(
            dict(
                block_id=str(uuid5(store.operation, role + ":block:" + str(i))),
                kind="paragraph",
                text_range=[match.start(), match.end()],
                original_anchor=dict(
                    basis="extracted_text_only_no_verified_dom_or_pdf_anchor",
                    extracted_text_sha256=text_blob.sha256,
                ),
                warnings=["original_document_location_unverified"],
            )
        )
    warnings = extracted.warnings + reasons
    configuration = dict(acquisition=profile.model_dump(mode="json"), extractor=extracted.name)
    verified = [text_proof] + ([original_proof] if original_proof is not None else [])
    prepared = prepare(store.repo, store.project, "SourceRevision", source.payload, verified=verified)

    def publish(publication):
        completed_source = enrich_source(publication, role, source, extracted)
        extractor_profile = publication.create(
            role + ":extractor-profile",
            "ProcessingProfile",
            dict(
                profile_kind="extraction",
                implementation=extracted.name,
                version=extracted.version,
                configuration=configuration,
                input_profile_refs=[request.profile_ref.model_dump(mode="json")],
            ),
        )
        payload = SourceRepresentation(
            source_revision_ref=completed_source.ref,
            representation_kind="document_layout"
            if extracted.name in ("marker", "pypdfium2", "PyPDF2")
            else "html_text",
            content_blob=text_blob,
            canonical_text_blob=text_blob,
            extractor=dict(
                profile_ref=extractor_profile.ref,
                name=extracted.name,
                version=extracted.version,
                configuration_hash=digest(configuration),
            ),
            normalization_version="preserve-extractor-text/1",
            blocks=blocks,
            extraction_report=dict(
                coverage=extracted.coverage, missing_regions=[], reading_order_issues=[], warnings=warnings
            ),
            language=request.language,
            is_transformation=extracted.is_transformation,
        )
        revision = publication.create(key, "SourceRepresentation", payload.model_dump(mode="json"))
        value = dict(
            representation_ref=revision.ref.model_dump(mode="json"),
            status=state,
            diagnostics=warnings,
            title=extracted.title,
            published_date=extracted.published_date,
        )
        publication.put(key, value)
        result = AcquiredSource(
            source_revision_ref=completed_source.ref,
            representation_refs=[revision.ref],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status=state,
            diagnostics=warnings,
        )
        outcome = dict(source=result.model_dump(mode="json"))
        publication.put(role + ":outcome", outcome)
        return outcome

    return store.publication(publish, prepared=prepared)


def enrich_source(publication, role, source, extracted):
    if not extracted.title and not extracted.published_date:
        return source
    payload = source.payload
    date = (
        SourceDate(value=extracted.published_date, precision="unknown", basis="reported_by_" + extracted.name)
        if extracted.published_date
        else None
    )
    enriched = SourceRevision.model_validate(payload).model_copy(
        update=dict(
            title=extracted.title,
            published_at=date,
            source_metadata_evidence=payload["source_metadata_evidence"]
            + [
                dict(
                    metadata_source_revision_ref=source.ref.model_dump(mode="json"),
                    extractor=extracted.name,
                    title=extracted.title,
                    published_date=extracted.published_date,
                )
            ],
        )
    )
    return publication.create(role + ":metadata-source", "SourceRevision", enriched.model_dump(mode="json"))
