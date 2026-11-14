"""Persist full extractor output and exact text ranges, with no inferred original anchors."""

import re
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
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
    if saved:
        previous = store.repo.get(store.project, ArtifactRef.model_validate(saved["representation_ref"]))
        result = AcquiredSource(
            source_revision_ref=previous.payload["source_revision_ref"],
            representation_refs=[previous.ref],
            role=role,
            acquisition_record_ref=acquisition_ref,
            status=saved.get("status", availability(document, extracted, profile)[0]),
            diagnostics=saved.get("diagnostics", previous.payload["extraction_report"]["warnings"]),
        )
        value = dict(source=result.model_dump(mode="json"))
        store.put(role + ":outcome", value)
        return value
    state, reasons = availability(document, extracted, profile)
    return publish_document(
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
        state,
        reasons,
    )


def publish_document(
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
    state,
    reasons,
):
    from podgen.persistence.acquisition_policy import SOURCE
    from podgen.persistence.archive_authorization import archive_write
    from podgen.persistence.artifact_batch import publish
    from podgen.persistence.publication_plan import ArtifactNode, PublicationBatch

    from .source_payload import enriched, received

    source_body = (
        source.payload
        if source is not None
        else received(
            request,
            role,
            document,
            original_proof.ref,
            acquisition_ref,
        )
    )
    nodes, external = [], {}
    if extracted is not None:
        source_body = enriched(
            source_body,
            extracted,
            store.ref(role + ":extracted"),
            prior=source.ref if source is not None else None,
        )
    source_key = role + ":source"
    source_changed = source is None or source_body != source.payload
    if source_changed:
        operation_key = role + ":metadata-source" if source is not None else source_key
        nodes.append(
            ArtifactNode(
                source_key,
                "SourceRevision",
                source_body,
                store.producer(operation_key),
                uuid5(store.operation, operation_key),
                reference_policy=SOURCE,
            )
        )
    else:
        external[source_key] = source.ref
    source_ref = {"$artifact": source_key}
    representation_refs = []
    proofs = [original_proof] if original_proof is not None else []
    if extracted is not None:
        configuration = dict(acquisition=profile.model_dump(mode="json"), extractor=extracted.name)
        profile_body = dict(
            profile_kind="extraction",
            implementation=extracted.name,
            version=extracted.version,
            configuration=configuration,
            input_profile_refs=[request.profile_ref.model_dump(mode="json")],
        )
        identity = digest(profile_body)
        operation = uuid5(store.project, "source-extractor-profile/1:" + identity)
        from podgen.domain.contracts.common import CodeProducer

        nodes.append(
            ArtifactNode(
                "extractor-profile",
                "ProcessingProfile",
                profile_body,
                CodeProducer(
                    kind="code",
                    component="source-extractor-configuration",
                    build_id="source-extractor-profile/1",
                    operation_id=operation,
                ),
                operation,
                artifact_id=uuid5(store.project, "source-extractor-profile/1:" + identity),
            )
        )
        blocks = []
        for i, match in enumerate(re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|$)", extracted.text, re.DOTALL)):
            blocks.append(
                dict(
                    block_id=str(uuid5(store.operation, role + ":block:" + str(i))),
                    kind="paragraph",
                    text_range=[match.start(), match.end()],
                    original_anchor=dict(
                        basis="extracted_text_only_no_verified_dom_or_pdf_anchor",
                        extracted_text_sha256=text_proof.ref.sha256,
                    ),
                    warnings=["original_document_location_unverified"],
                )
            )
        representation_key = role + ":representation"
        payload = dict(
            source_revision_ref=source_ref,
            representation_kind="document_layout"
            if extracted.name in ("marker", "pypdfium2", "PyPDF2")
            else "html_text",
            content_blob=text_proof.ref.model_dump(mode="json"),
            canonical_text_blob=text_proof.ref.model_dump(mode="json"),
            extractor=dict(
                profile_ref={"$artifact": "extractor-profile"},
                name=extracted.name,
                version=extracted.version,
                configuration_hash=digest(configuration),
            ),
            normalization_version="preserve-extractor-text/1",
            blocks=blocks,
            extraction_report=dict(
                coverage=extracted.coverage,
                missing_regions=[],
                reading_order_issues=[],
                warnings=extracted.warnings + reasons,
            ),
            language=request.language,
            is_transformation=extracted.is_transformation,
        )
        nodes.append(
            ArtifactNode(
                representation_key,
                "SourceRepresentation",
                payload,
                store.producer(representation_key),
                uuid5(store.operation, representation_key),
            )
        )
        representation_refs.append({"$artifact": representation_key})
        proofs.append(text_proof)
    prepared = prepare(store.repo, store.project, "SourceRevision", source_body, verified=proofs)
    outcome_key = role + ":outcome"
    source_value = dict(
        source_revision_ref=source_ref,
        representation_refs=representation_refs,
        role=role,
        acquisition_record_ref=acquisition_ref.model_dump(mode="json"),
        status=state,
        diagnostics=(extracted.warnings if extracted is not None else []) + reasons,
    )
    runtime_nodes = [store.node(outcome_key, dict(source=source_value))]
    completed = (
        store.completion(role, source_value, extracted.text if extracted is not None else None)
        if store.completion
        else None
    )
    if completed is not None:
        runtime_nodes.append(store.node("result", completed))
    batch = PublicationBatch(
        artifacts=tuple(nodes), runtimes=tuple(runtime_nodes), external_artifacts=external, prepared=prepared
    )
    values = archive_write(
        store.repo, store.project, lambda tx: publish(store.repo, tx, store.project, batch)
    )
    if "result" in values.payloads:
        store.completed = values.payloads["result"]
    return values.payloads[outcome_key]
