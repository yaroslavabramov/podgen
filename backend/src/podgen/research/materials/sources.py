"""Keep reported URLs, acquisition state and readable representations as distinct facts."""

import re
from urllib.parse import urlsplit

from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.errors import ContractError
from podgen.writing.editorial_contract import SourceDocument

from .contracts import ResearchSource


def freeze_sources(repo, project, result, mapping, selections, assessments):
    acquired = {}
    for _, selection in selections:
        for row in selection["sources"]:
            url = row["reported_url"]
            if url in acquired and acquired[url] != row:
                raise ContractError("research_conflicting_acquired_source_versions")
            acquired[url] = row
    reported = {}
    for item in result["material"]["reported_sources"]:
        reported.setdefault(item["url"], []).append(item)
    numbered = [(number, row["url"], row["label"]) for number, row in sorted(mapping.items())]
    known = {url for _, url, _ in numbered}
    next_number = max(mapping, default=0) + 1
    for url in reported:
        if url not in known:
            numbered.append((None, url, urlsplit(url).hostname or "Reported source"))
    overrides = {row.report_number: row for row in assessments}
    if len(overrides) != len(assessments):
        raise ContractError("research_duplicate_source_assessment")
    if set(overrides) - set(mapping):
        raise ContractError("research_assessment_unknown_source_number")
    sources, documents, bindings = [], [], {}
    for number, url, label in numbered:
        identity = "S" + str(number if number is not None else next_number)
        if number is None:
            next_number += 1
        values = dict(
            source_id=identity,
            reported_url=url,
            report_number=number,
            reported_label=label,
            title=label or "Reported source",
            status="not_acquired",
            original_occurrences=reported.get(url, []),
        )
        admission = acquired.get(url)
        if admission:
            outcome = admission["acquisition"]
            values.update(
                status="unavailable",
                acquisition_status=outcome["status"],
                limitations=list(outcome["diagnostics"]),
            )
            # Prefer a readable original, then the first readable retained fallback in acquisition order.
            versions = sorted(
                enumerate(outcome["sources"]), key=lambda item: (item[1]["role"] != "original", item[0])
            )
            selected = next(
                (
                    row
                    for _, row in versions
                    if row["status"] in ("available", "unconfirmed") and row["representation_refs"]
                ),
                None,
            )
            if selected is None:
                selected = next((row for _, row in versions if row["role"] == "original"), None)
            if selected:
                source_ref = ArtifactRef.model_validate(selected["source_revision_ref"])
                metadata = repo.get(project, source_ref).payload
                values.update(
                    acquisition_role=selected["role"],
                    source_revision_ref=source_ref,
                    acquisition_record_ref=selected["acquisition_record_ref"],
                    title=(metadata.get("title") or label)
                    if selected["status"] in ("available", "unconfirmed")
                    else label,
                    title_origin="source_metadata"
                    if selected["status"] in ("available", "unconfirmed") and metadata.get("title")
                    else "reported_label",
                    resolved_url=metadata["resolved_uri"],
                    limitations=values["limitations"] + selected["diagnostics"] + metadata["limitations"],
                )
                if selected["status"] in ("available", "unconfirmed") and selected["representation_refs"]:
                    if len(selected["representation_refs"]) != 1:
                        raise ContractError("research_select_one_source_representation")
                    ref = ArtifactRef.model_validate(selected["representation_refs"][0])
                    representation = repo.get(project, ref).payload
                    if representation["source_revision_ref"] != source_ref.model_dump(mode="json"):
                        raise ContractError("research_source_representation_mismatch")
                    blob = BlobRef.model_validate(
                        representation["canonical_text_blob"] or representation["content_blob"]
                    )
                    text = repo.read_blob(project, ref, blob).decode("utf-8")
                    heading = re.match(r"\s*# ([^\r\n]+)", text)
                    if heading:
                        values.update(
                            title=heading[1], title_origin="retained_heading", title_range=heading.span(1)
                        )
                    key = "source-" + identity
                    values.update(
                        status="available" if selected["status"] == "available" else "partial",
                        document=key,
                        representation_ref=ref,
                        coverage=representation["extraction_report"]["coverage"],
                    )
                    documents.append(
                        SourceDocument(
                            key=key,
                            title=values["title"],
                            source_path="research/sources/" + identity + ".txt",
                            sha256=blob.sha256,
                            purpose="primary",
                        )
                    )
                    bindings[key] = dict(ref=ref.model_dump(mode="json"), blob=blob.model_dump(mode="json"))
        if number in overrides:
            assessment = overrides[number]
            if values["status"] == "not_acquired":
                raise ContractError("research_cannot_assess_unacquired_source")
            if assessment.status == "partial" and not values.get("document"):
                raise ContractError("research_partial_source_requires_readable_text")
            values["status"] = assessment.status
            values["limitations"].append(assessment.observation)
            if assessment.status == "unavailable" and values.get("document"):
                key = values.pop("document")
                documents = [doc for doc in documents if doc.key != key]
                bindings.pop(key)
        sources.append(ResearchSource(**values))
    return sources, documents, bindings
