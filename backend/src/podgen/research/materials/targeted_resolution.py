"""Acquire only the narrow answer's cited documents and validate its literal excerpts."""

from podgen.domain.errors import ContractError
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_report
from .targeted_reparse import VERSION


def resolve(resolver, store, key, child, arguments, guard):
    from .targeted import handoff, parse, validated_excerpts

    report = child.bytes("result", child.get("result")["report_text_blob"]).decode("utf-8")
    try:
        answer = parse(report)
    except ContractError as error:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unresolved",
                error=error.code,
                question=arguments["question"],
                next_action="The saved research answer failed validation. Continue this assessment with source discovery and reading; no completed answer was admitted.",
            ),
        )
    if not answer.evidence:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unresolved",
                question=arguments["question"],
                answer=answer.answer,
                limitations=answer.limitations,
                next_action="No original-source excerpt was supplied. Continue verification; no completed answer was admitted.",
            ),
        )
    sources, source_map, resolved = [], [], []
    for url in dict.fromkeys(row.url for row in answer.evidence):
        guard()
        resolution = resolver.document(store, url, guard)
        source_id = resolution.result.get("source_id")
        resolved.append((url, source_id))
        source_map.append(
            dict(
                reported_url=url,
                report_numbers=[],
                source_id=source_id,
                availability=resolution.result.get(
                    "availability", resolution.result.get("status", "unavailable")
                ),
            )
        )
    unavailable = [row for row in source_map if row["source_id"] is None]
    admissions = {a.source_id: a for a in resolver.restore(run_id=store.run_id, guard=guard)}
    by_path = {d.path: d for d in resolver.materials.documents}
    if len(by_path) != len(resolver.materials.documents):
        raise ContractError("targeted_research_document_binding_mismatch")
    documents = {}
    for spec in resolver.materials.packet.documents:
        document = by_path.get(spec.source_path)
        if document is None or document.sha256 != spec.sha256:
            raise ContractError("targeted_research_document_binding_mismatch")
        documents[spec.key] = document
    for url, source_id in resolved:
        admission = admissions.get(source_id)
        if admission is None and source_id is not None:
            from podgen.sources.material_requests import SourceAdmission

            known = next(s for s in resolver.materials.packet.sources if s.source_id == source_id)
            document = documents[known.document]
            admission = SourceAdmission(
                source_id,
                known.title,
                document.text,
                document.sha256,
                document.ref,
                known.model_dump(mode="json"),
            )
        if admission is not None:
            sources.append((url, admission))
    excerpts, rejected = validated_excerpts(answer, sources)
    if rejected:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                contract="targeted-research-partial-evidence/1",
                status="unresolved" if excerpts or not unavailable else "unavailable",
                error="targeted_research_evidence_unavailable" if unavailable else rejected[0]["error"],
                question=arguments["question"],
                unverified_answer=answer.answer,
                limitations=answer.limitations,
                excerpts=excerpts,
                rejected_evidence=rejected,
                sources=source_map,
                relationship="validated_source_locations_not_read_receipts",
                next_action="The answer remains unverified. Read the validated excerpt ranges with read_source and resolve the rejected evidence before deciding the claim; no completed answer was admitted.",
            ),
        )
    summary = handoff(answer, excerpts)
    store.put(key + ":answer", summary)
    admitted_report = admit_report(store, resolver.materials, child, arguments["question"], source_map)
    return MaterialResolution(
        dict(
            status="resolved",
            validation_contract=VERSION,
            report_id=admitted_report.source_id,
            title=admitted_report.title,
            **summary,
        ),
        resolver.restore(run_id=store.run_id, guard=guard),
    )


def _unfinished(resolver, store, guard, result):
    return MaterialResolution(
        dict(result, validation_contract=VERSION), resolver.restore(run_id=store.run_id, guard=guard)
    )
