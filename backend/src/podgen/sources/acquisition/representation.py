"""Persist full extractor output and exact text ranges, with no inferred original anchors."""

import re
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.evidence import SourceRepresentation
from podgen.sources.extraction.quality import assess_web_fetch_health


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


def save_representation(store, request, role, source_ref, document, extracted, profile):
    key = role + ":representation"
    saved = store.get(key)
    if saved:
        return saved
    state, reasons = availability(document, extracted, profile)
    text_blob = store.blob(extracted.text.encode("utf-8"), "text/plain; charset=utf-8")
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
    extractor_profile_ref = store.create(
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
        source_revision_ref=source_ref,
        representation_kind="document_layout"
        if extracted.name in ("marker", "pypdfium2", "PyPDF2")
        else "html_text",
        content_blob=text_blob,
        canonical_text_blob=text_blob,
        extractor=dict(
            profile_ref=extractor_profile_ref,
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
    ref = store.create(key, "SourceRepresentation", payload.model_dump(mode="json"))
    value = dict(
        representation_ref=ref.model_dump(mode="json"),
        status=state,
        diagnostics=warnings,
        title=extracted.title,
        published_date=extracted.published_date,
    )
    store.put(key, value)
    return value
