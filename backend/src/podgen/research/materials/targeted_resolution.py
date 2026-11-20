"""Acquire only the narrow answer's cited documents and validate its literal excerpts."""

from podgen.domain.errors import ContractError
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_report


def resolve(resolver, store, key, child, arguments, guard):
    from .targeted import compact, parse

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
                availability=resolution.result.get("availability", resolution.result["status"]),
            )
        )
    unavailable = [row for row in source_map if row["source_id"] is None]
    if unavailable:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unavailable",
                error="targeted_research_evidence_unavailable",
                question=arguments["question"],
                sources=unavailable,
                next_action="Resolve the unavailable evidence or use another source; no completed answer was admitted.",
            ),
        )
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
    try:
        summary = compact(answer, sources)
    except ContractError as error:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unresolved",
                error=error.code,
                question=arguments["question"],
                sources=source_map,
                next_action="The saved research excerpt failed validation. Inspect these retained documents with source search and reading tools; no completed answer was admitted.",
            ),
        )
    store.put(key + ":answer", summary)
    admitted_report = admit_report(store, resolver.materials, child, arguments["question"], source_map)
    return MaterialResolution(
        dict(status="resolved", report_id=admitted_report.source_id, title=admitted_report.title, **summary),
        resolver.restore(run_id=store.run_id, guard=guard),
    )


def _unfinished(resolver, store, guard, result):
    return MaterialResolution(result, resolver.restore(run_id=store.run_id, guard=guard))
